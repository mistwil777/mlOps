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
from pipeline.utils.metrics import rouge_l_score

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


def _is_valid_response(text: str) -> bool:
    """Heuristic: non-empty, not an error message, has some structure."""
    if not text or len(text.strip()) < 10:
        return False
    error_patterns = ["error:", "traceback", "exception:", "i'm sorry", "i cannot"]
    lower = text.lower()
    return not any(p in lower for p in error_patterns)


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
        pass_count = 0
        rouge_scores: list[float] = []

        for r in records:
            completion, latency = _call_local_model(r["prompt"], model_path, port)
            latencies.append(latency)

            valid = _is_valid_response(completion)
            if valid:
                pass_count += 1

            rl = rouge_l_score(completion, r["completion"])
            rouge_scores.append(rl)

            results.append({
                "prompt": r["prompt"][:200],
                "reference": r["completion"][:200],
                "hypothesis": completion[:200],
                "valid_response": valid,
                "rouge_l": rl,
                "latency_ms": latency,
            })

        latencies_sorted = sorted(latencies)
        p95_idx = max(0, int(len(latencies_sorted) * 0.95) - 1)

        metrics = {
            "n": len(records),
            "pass_rate": pass_count / len(records) if records else 0,
            "rouge_l": sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0,
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
        pass_count = 0
        rouge_scores: list[float] = []

        for r in records:
            completion, latency = _call_nova_pro(r["prompt"])
            latencies.append(latency)
            if _is_valid_response(completion):
                pass_count += 1
            rouge_scores.append(rouge_l_score(completion, r["completion"]))

        latencies_sorted = sorted(latencies)
        p95_idx = max(0, int(len(latencies_sorted) * 0.95) - 1)

        return {"metrics": {
            "n": len(records),
            "pass_rate": pass_count / len(records) if records else 0,
            "rouge_l": sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0,
            "latency_p50": sorted(latencies)[len(latencies)//2] if latencies else 0,
            "latency_p95": latencies_sorted[p95_idx] if latencies else 0,
        }}

    def check_quality_gates(self, metrics: dict[str, float]) -> dict[str, bool]:
        thresholds = self.config.get("quality_thresholds", {})
        gates: dict[str, bool] = {}

        if "min_pass_rate" in thresholds:
            gates["pass_rate"] = metrics.get("pass_rate", 0) >= thresholds["min_pass_rate"]

        if "min_rouge_l" in thresholds:
            gates["rouge_l"] = metrics.get("rouge_l", 0) >= thresholds["min_rouge_l"]

        if "max_latency_ratio" in thresholds:
            baseline_p50 = self.config.get("baseline", {}).get("metrics", {}).get("latency_p50", 0)
            if baseline_p50 > 0:
                ratio = metrics.get("latency_p50", 0) / baseline_p50
                gates["latency_ratio"] = ratio <= thresholds["max_latency_ratio"]

        return gates

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

        all_passed = all(gates.values()) if gates else True

        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Eval Report — {self.project}</title>
<style>body{{font-family:monospace;margin:2em}} table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:8px}}
.pass{{color:green}} .fail{{color:red}}</style></head><body>
<h1>Evaluation Report — {self.project}</h1>
<p>Samples: {metrics.get('n', 'N/A')}</p>
<table><tr><th>Metric</th><th>Value</th></tr>{rows}</table>
<p>Gates: {"ALL PASSED" if all_passed else "FAILED: " + ", ".join(k for k,v in gates.items() if not v)}</p>
</body></html>"""

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(html, encoding="utf-8")
        logger.info("Eval report written to %s", output_path)
