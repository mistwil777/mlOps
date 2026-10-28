"""Model evaluation: run test set, compare with baseline, generate HTML report."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.evaluators import evaluate as _evaluate_pair
from pipeline.utils.jsonl import read_jsonl
from pipeline.utils.llm import call_llm as _call_llm
from pipeline.utils.logger import get_logger, log_metric
from pipeline.utils.metrics import rouge_l_score
from pipeline.utils.stats import wilson_ci, sample_size_warning

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
    """Call generation backend (Bedrock→Ollama fallback) and return (completion, latency_ms)."""
    t0 = time.perf_counter()
    completion = _call_llm(prompt, max_tokens=2048, temperature=0.1) or ""
    latency = (time.perf_counter() - t0) * 1000
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
        domain: str | None = None,
    ) -> dict[str, Any]:
        if domain is None:
            domain = self.config.get("domain", "code")
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

            reference = r.get("completion")
            eval_result = _evaluate_pair(completion, reference, domain)
            if eval_result.correct:
                pass_count += 1

            rl = rouge_l_score(completion, reference) if reference else 0.0
            rouge_scores.append(rl)

            results.append({
                "prompt": r["prompt"][:200],
                "reference": (reference or "")[:200],
                "hypothesis": completion[:200],
                "valid_response": eval_result.correct,
                "evaluator_method": eval_result.method,
                "evaluator_score": eval_result.score,
                "evaluator_detail": eval_result.detail,
                "rouge_l": rl,
                "latency_ms": latency,
            })

        latencies_sorted = sorted(latencies)
        p95_idx = max(0, int(len(latencies_sorted) * 0.95) - 1)
        n = len(records)

        pass_rate = pass_count / n if n else 0.0
        rouge_mean = sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0.0
        exact_match_k = sum(1 for r in results if r["reference"] == r["hypothesis"])

        # Wilson CI on pass_rate and rouge_l (treated as binary at threshold 0.5)
        pass_ci = wilson_ci(pass_count, n)
        rouge_above = sum(1 for s in rouge_scores if s >= 0.5)
        rouge_ci = wilson_ci(rouge_above, n)

        metrics = {
            "n": n,
            "pass_rate": round(pass_rate, 4),
            "pass_rate_ci_low": pass_ci.low,
            "pass_rate_ci_high": pass_ci.high,
            "pass_rate_margin": pass_ci.margin,
            "rouge_l": round(rouge_mean, 4),
            "rouge_l_ci_low": rouge_ci.low,
            "rouge_l_ci_high": rouge_ci.high,
            "latency_p50": sorted(latencies)[len(latencies)//2] if latencies else 0,
            "latency_p95": latencies_sorted[p95_idx] if latencies else 0,
            "exact_match": round(exact_match_k / n, 4) if n else 0.0,
        }

        sample_warn = sample_size_warning(n)
        metrics["sample_size_sufficient"] = sample_warn["sufficient"]
        metrics["sample_size_message"] = sample_warn["message"]

        for k, v in metrics.items():
            if isinstance(v, (int, float)):
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
