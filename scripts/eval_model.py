"""Domain-aware model evaluation — logs all metrics to MLflow."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import mlflow
import yaml


def _load_domain_config(domain: str) -> dict[str, Any]:
    path = Path("evals/domains") / f"{domain}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Domain config not found: {path}")
    return yaml.safe_load(path.read_text())


def _load_eval_pairs(source_dir: Path, test_fraction: float, seed: int) -> list[dict[str, str]]:
    jsonl_files = sorted(source_dir.glob("*.jsonl"))
    pairs: list[dict[str, str]] = []
    for f in jsonl_files:
        for line in f.read_text().splitlines():
            line = line.strip()
            if line:
                pairs.append(json.loads(line))

    rng = random.Random(seed)
    rng.shuffle(pairs)
    n_test = max(1, int(len(pairs) * test_fraction))
    return pairs[-n_test:]


def _call_mlx_server(port: int, prompt: str, system: str | None = None) -> tuple[str, float]:
    import httpx

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    t0 = time.perf_counter()
    resp = httpx.post(
        f"http://localhost:{port}/v1/chat/completions",
        json={"model": "local", "messages": messages, "max_tokens": 2048, "temperature": 0.0},
        timeout=120.0,
    )
    resp.raise_for_status()
    latency = time.perf_counter() - t0
    content = resp.json()["choices"][0]["message"]["content"]
    return content, latency


def _compute_rouge_l(prediction: str, reference: str) -> float:
    try:
        from rouge_score import rouge_scorer  # type: ignore[import]
        scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)
        score = scorer.score(reference, prediction)
        return score["rougeL"].fmeasure
    except ImportError:
        # Rough token-overlap fallback if rouge_score not installed
        pred_tokens = set(prediction.lower().split())
        ref_tokens = set(reference.lower().split())
        if not ref_tokens:
            return 0.0
        return len(pred_tokens & ref_tokens) / len(ref_tokens)


def _compute_pass_rate(predictions: list[str], references: list[str]) -> float:
    # Simple exact-match or near-exact; override with domain test harness when available
    passes = sum(
        1 for p, r in zip(predictions, references)
        if p.strip() and r.strip() and (p.strip() == r.strip() or _compute_rouge_l(p, r) >= 0.90)
    )
    return passes / len(predictions) if predictions else 0.0


def _check_java_compilation(java_snippet: str) -> bool:
    """Write snippet to temp file and attempt javac compilation."""
    import tempfile
    tmp = Path(tempfile.mktemp(suffix=".java"))
    # Wrap bare snippet in a class if it doesn't have one
    if "class " not in java_snippet:
        java_snippet = f"public class Tmp {{\n{java_snippet}\n}}"
    tmp.write_text(java_snippet)
    result = subprocess.run(
        ["javac", str(tmp)], capture_output=True, timeout=10
    )
    tmp.unlink(missing_ok=True)
    # Remove .class file if it was created
    class_file = tmp.with_suffix(".class")
    class_file.unlink(missing_ok=True)
    return result.returncode == 0


def _compute_compilation_rate(predictions: list[str]) -> float:
    results = []
    for p in predictions:
        try:
            results.append(_check_java_compilation(p))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            results.append(False)
    return sum(results) / len(results) if results else 0.0


def run_eval(
    project: str,
    domain: str,
    model_port: int,
    mlflow_run_id: str | None = None,
    experiment_name: str = "mlops-eval",
    system_prompt: str | None = None,
) -> dict[str, float]:
    domain_cfg = _load_domain_config(domain)
    split_cfg = domain_cfg.get("eval_split", {})
    source_dir = Path(split_cfg.get("source", f"datalake/projects/{project}/fine-tuning-ready/"))
    test_fraction = split_cfg.get("test_fraction", 0.15)
    seed = split_cfg.get("seed", 42)
    min_samples = split_cfg.get("min_samples", 20)

    pairs = _load_eval_pairs(source_dir, test_fraction, seed)
    if len(pairs) < min_samples:
        print(f"WARNING: only {len(pairs)} eval pairs (min_samples={min_samples})")

    print(f"Evaluating {len(pairs)} pairs — project={project} domain={domain} port={model_port}")

    predictions: list[str] = []
    references: list[str] = []
    latencies: list[float] = []

    for i, pair in enumerate(pairs):
        prompt = pair.get("prompt") or pair.get("instruction") or pair.get("input", "")
        reference = pair.get("completion") or pair.get("output") or pair.get("response", "")
        try:
            pred, latency = _call_mlx_server(model_port, prompt, system_prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"  [pair {i}] ERROR: {exc}")
            pred, latency = "", 0.0
        predictions.append(pred)
        references.append(reference)
        latencies.append(latency)

    metrics: dict[str, float] = {}

    # Universal metrics
    metrics["pass_rate"] = _compute_pass_rate(predictions, references)
    metrics["rouge_l"] = sum(_compute_rouge_l(p, r) for p, r in zip(predictions, references)) / len(pairs)
    metrics["avg_latency_s"] = sum(latencies) / len(latencies) if latencies else 0.0

    # Domain-specific metrics
    metric_names = [m["name"] for m in domain_cfg.get("metrics", [])]
    if "compilation_rate" in metric_names:
        metrics["compilation_rate"] = _compute_compilation_rate(predictions)
    if "codebleu" in metric_names:
        try:
            from codebleu import calc_codebleu  # type: ignore[import]
            result = calc_codebleu(references, predictions, lang="java", weights=(0.25, 0.25, 0.25, 0.25))
            metrics["codebleu"] = result["codebleu"]
        except ImportError:
            print("  WARNING: codebleu not installed — skipping CodeBLEU metric")

    # Log to MLflow
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_id=mlflow_run_id) as run:
        mlflow.set_tag("project", project)
        mlflow.set_tag("domain", domain)
        mlflow.set_tag("eval_pairs", str(len(pairs)))
        for k, v in metrics.items():
            mlflow.log_metric(k, v)
        print(f"  MLflow run: {run.info.run_id}")

    print("Metrics:")
    for k, v in metrics.items():
        print(f"  {k}: {v:.4f}")

    return metrics


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Domain-aware model eval")
    parser.add_argument("--project", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--port", type=int, default=8080, help="mlx_lm.server port")
    parser.add_argument("--run-id", help="Existing MLflow run ID to attach metrics to")
    parser.add_argument("--experiment", default="mlops-eval")
    parser.add_argument("--system-prompt", help="System prompt for inference")
    args = parser.parse_args()

    metrics = run_eval(
        project=args.project,
        domain=args.domain,
        model_port=args.port,
        mlflow_run_id=args.run_id,
        experiment_name=args.experiment,
        system_prompt=args.system_prompt,
    )
    thresholds_path = Path("evals/domains") / f"{args.domain}.yaml"
    gate_thresholds = yaml.safe_load(thresholds_path.read_text()).get("gate_thresholds", {})
    failed = False
    for key, threshold in gate_thresholds.items():
        metric_key = key.removeprefix("min_").removeprefix("max_")
        if metric_key not in metrics:
            continue
        actual = metrics[metric_key]
        if key.startswith("min_") and actual < threshold:
            print(f"BELOW THRESHOLD: {metric_key}={actual:.4f} < {threshold:.4f}")
            failed = True
        elif key.startswith("max_") and actual > threshold:
            print(f"ABOVE THRESHOLD: {metric_key}={actual:.4f} > {threshold:.4f}")
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
