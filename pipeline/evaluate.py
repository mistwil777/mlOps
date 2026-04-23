"""Model evaluation: run test set, compare with baseline, generate HTML report."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl
from pipeline.utils.logger import get_logger, log_metric
from pipeline.utils.metrics import compile_java, codebLEU_score

logger = get_logger("mlops.evaluate")


def _load_project_config(project: str) -> dict[str, Any]:
    with open(Path("config/projects") / f"{project}.yaml") as fh:
        return yaml.safe_load(fh)


def _call_local_model(prompt: str, model_path: str, port: int = 8080) -> tuple[str, float]:
    """Call mlx_lm.server and return (completion, latency_ms)."""
    import httpx  # noqa: PLC0415

    t0 = time.perf_counter()
    try:
        resp = httpx.post(
            f"http://localhost:{port}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2048,
                "temperature": 0.1,
            },
            timeout=60.0,
        )
        latency = (time.perf_counter() - t0) * 1000
        completion = resp.json()["choices"][0]["message"]["content"]
        return completion, latency
    except Exception as exc:  # noqa: BLE001
        logger.error("Local model call failed: %s", exc)
        return "", (time.perf_counter() - t0) * 1000


def _call_nova_pro(prompt: str) -> tuple[str, float]:
    """Call AWS Bedrock Nova Pro and return (completion, latency_ms)."""
    import boto3  # noqa: PLC0415

    client = boto3.client(
        "bedrock-runtime",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )
    model_id = os.environ.get("AWS_BEDROCK_MODEL_GENERATION", "us.amazon.nova-pro-v1:0")

    t0 = time.perf_counter()
    response = client.invoke_model(
        modelId=model_id,
        body=json.dumps({
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 2048, "temperature": 0.1},
        }),
        contentType="application/json",
        accept="application/json",
    )
    latency = (time.perf_counter() - t0) * 1000
    body = json.loads(response["body"].read())
    completion = body["output"]["message"]["content"][0]["text"]
    return completion, latency


class ModelEvaluator:
    def __init__(self, project: str):
        self.project = project
        self.config = _load_project_config(project)

    def run_test_set(
        self,
        model_path: str,
        test_jsonl: Path | str,
        n: int = 50,
        port: int = 8080,
    ) -> dict[str, Any]:
        records = read_jsonl(test_jsonl)
        if n < len(records):
            import random  # noqa: PLC0415
            random.seed(42)
            records = random.sample(records, n)

        results = []
        latencies: list[float] = []
        compilation_ok = 0
        codebleu_scores: list[float] = []

        for r in records:
            completion, latency = _call_local_model(r["prompt"], model_path, port)
            latencies.append(latency)

            compiled, _ = compile_java(completion)
            if compiled:
                compilation_ok += 1

            bleu = codebLEU_score(completion, r["completion"])
            codebleu_scores.append(bleu)

            results.append({
                "prompt": r["prompt"][:200],
                "reference": r["completion"][:200],
                "hypothesis": completion[:200],
                "compiled": compiled,
                "codebleu": bleu,
                "latency_ms": latency,
            })

        latencies_sorted = sorted(latencies)
        p95_idx = max(0, int(len(latencies_sorted) * 0.95) - 1)

        metrics = {
            "n": len(records),
            "java_compilation": compilation_ok / len(records) if records else 0,
            "codebleu": sum(codebleu_scores) / len(codebleu_scores) if codebleu_scores else 0,
            "latency_p50": sorted(latencies)[len(latencies)//2] if latencies else 0,
            "latency_p95": latencies_sorted[p95_idx] if latencies else 0,
            "exact_match": sum(
                1 for r in results if r["reference"] == r["hypothesis"]
            ) / len(results) if results else 0,
        }

        for k, v in metrics.items():
            log_metric(k, v)

        return {"metrics": metrics, "samples": results[:10]}

    def compare_with_baseline(
        self,
        model_path: str,
        baseline_name: str,
        test_jsonl: Path | str,
        n: int = 50,
    ) -> dict[str, Any]:
        model_results = self.run_test_set(model_path, test_jsonl, n)

        if baseline_name == "nova-pro":
            baseline_results = self._run_nova_baseline(test_jsonl, n)
        else:
            baseline_results = self.run_test_set(baseline_name, test_jsonl, n)

        delta: dict[str, float] = {}
        for k in model_results["metrics"]:
            if k == "n":
                continue
            model_val = model_results["metrics"][k]
            base_val = baseline_results["metrics"].get(k, 0)
            delta[k] = model_val - base_val

        return {
            "model": model_results["metrics"],
            "baseline": baseline_results["metrics"],
            "delta": delta,
        }

    def _run_nova_baseline(self, test_jsonl: Path | str, n: int) -> dict[str, Any]:
        records = read_jsonl(test_jsonl)
        if n < len(records):
            import random  # noqa: PLC0415
            random.seed(42)
            records = random.sample(records, n)

        latencies: list[float] = []
        compilation_ok = 0
        codebleu_scores: list[float] = []

        for r in records:
            completion, latency = _call_nova_pro(r["prompt"])
            latencies.append(latency)
            compiled, _ = compile_java(completion)
            if compiled:
                compilation_ok += 1
            codebleu_scores.append(codebLEU_score(completion, r["completion"]))

        latencies_sorted = sorted(latencies)
        p95_idx = max(0, int(len(latencies_sorted) * 0.95) - 1)

        return {"metrics": {
            "n": len(records),
            "java_compilation": compilation_ok / len(records) if records else 0,
            "codebleu": sum(codebleu_scores) / len(codebleu_scores) if codebleu_scores else 0,
            "latency_p50": sorted(latencies)[len(latencies)//2] if latencies else 0,
            "latency_p95": latencies_sorted[p95_idx] if latencies else 0,
        }}

    def check_quality_gates(self, metrics: dict[str, float]) -> dict[str, bool]:
        thresholds = self.config.get("quality_thresholds", {})
        return {
            "java_compilation": metrics.get("java_compilation", 0) >= thresholds.get("min_java_compilation", 0.9),
            "codebleu": metrics.get("codebleu", 0) >= thresholds.get("min_codebLEU", 0.7),
        }

    def generate_eval_report(
        self,
        results: dict[str, Any],
        output_path: Path | str,
    ) -> None:
        metrics = results.get("metrics", results.get("model", {}))
        gates = self.check_quality_gates(metrics)

        rows = "".join(
            f"<tr><td>{k}</td><td>{v:.3f}" +
            (f" {'✓' if gates.get(k, True) else '✗'}" if k in gates else "") +
            "</td></tr>"
            for k, v in metrics.items() if k != "n"
        )

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Eval Report — {self.project}</title>
<style>body{{font-family:monospace;margin:2em}} table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:8px}}
.pass{{color:green}} .fail{{color:red}}</style></head><body>
<h1>Evaluation Report — {self.project}</h1>
<p>Samples: {metrics.get('n', 'N/A')}</p>
<table><tr><th>Metric</th><th>Value</th></tr>{rows}</table>
<p>Gates: {"ALL PASSED" if all(gates.values()) else "FAILED: " + ", ".join(k for k,v in gates.items() if not v)}</p>
</body></html>"""

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("Eval report written to %s", output_path)
