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
from pipeline.utils.kpi_store import KpiStore, RunRecord

logger = get_logger("mlops.evaluate")
_kpi_store = KpiStore()


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
        run_id: str | None = None,
        model_version: str = "v1",
        mlflow_run_id: str | None = None,
        adapter_path: str | None = None,
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

        # Persist to KPI dashboard
        import uuid  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        effective_run_id = run_id or f"run-{self.project}-{model_version}-{uuid.uuid4().hex[:8]}"
        eval_id = f"eval-{effective_run_id}"
        record = RunRecord(
            eval_id=eval_id,
            run_id=effective_run_id,
            project_id=self.project,
            model_version=model_version,
            eval_type="test_set",
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            model_path=adapter_path or model_path,
            pass_rate=metrics["pass_rate"],
            pass_rate_ci_low=metrics["pass_rate_ci_low"],
            pass_rate_ci_high=metrics["pass_rate_ci_high"],
            pass_rate_margin=metrics["pass_rate_margin"],
            rouge_l=metrics["rouge_l"],
            exact_match=metrics["exact_match"],
            latency_p50=metrics["latency_p50"],
            latency_p95=metrics["latency_p95"],
            n_samples=metrics["n"],
            sample_size_sufficient=metrics["sample_size_sufficient"],
            evaluator_method="domain-aware",
            notes=f"mlflow_run_id={mlflow_run_id}" if mlflow_run_id else None,
        )
        try:
            _kpi_store.save_evaluation(record)
            logger.info("KPI evaluation saved: run_id=%s project=%s", effective_run_id, self.project)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KPI save failed (non-blocking): %s", exc)

        return {"metrics": metrics, "samples": results[:10], "run_id": effective_run_id}

    def compare_with_baseline(
        self,
        model_path: str,
        baseline_name: str,
        test_jsonl: Path | str,
        n: int = 50,
        model_version: str = "v1",
        mlflow_run_id: str | None = None,
        adapter_path: str | None = None,
    ) -> dict[str, Any]:
        model_results = self.run_test_set(
            model_path, test_jsonl, n,
            model_version=model_version,
            mlflow_run_id=mlflow_run_id,
            adapter_path=adapter_path,
        )

        if baseline_name == "nova-pro":
            baseline_results = self._run_nova_baseline(test_jsonl, n)
        else:
            baseline_results = self.run_test_set(baseline_name, test_jsonl, n, model_version="baseline")

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

    def _run_nova_baseline(
        self,
        test_jsonl: Path | str,
        n: int,
        run_id: str | None = None,
        mlflow_run_id: str | None = None,
    ) -> dict[str, Any]:
        records = read_jsonl(test_jsonl)
        if n < len(records):
            import random  # noqa: PLC0415
            random.seed(42)
            records = random.sample(records, n)

        latencies: list[float] = []
        pass_count = 0
        rouge_scores: list[float] = []
        domain = self.config.get("domain", "code")

        for r in records:
            completion, latency = _call_nova_pro(r["prompt"])
            latencies.append(latency)
            eval_result = _evaluate_pair(completion, r.get("completion"), domain)
            if eval_result.correct:
                pass_count += 1
            rouge_scores.append(rouge_l_score(completion, r.get("completion", "")))

        latencies_sorted = sorted(latencies)
        p95_idx = max(0, int(len(latencies_sorted) * 0.95) - 1)
        nb = len(records)
        pass_ci = wilson_ci(pass_count, nb)

        metrics = {
            "n": nb,
            "pass_rate": round(pass_count / nb, 4) if nb else 0,
            "pass_rate_ci_low": pass_ci.low,
            "pass_rate_ci_high": pass_ci.high,
            "pass_rate_margin": pass_ci.margin,
            "rouge_l": round(sum(rouge_scores) / len(rouge_scores), 4) if rouge_scores else 0,
            "latency_p50": sorted(latencies)[len(latencies) // 2] if latencies else 0,
            "latency_p95": latencies_sorted[p95_idx] if latencies else 0,
            "exact_match": 0.0,
            "sample_size_sufficient": sample_size_warning(nb)["sufficient"],
        }

        import uuid  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        effective_run_id = run_id or f"run-{self.project}-baseline-nova-pro-{uuid.uuid4().hex[:8]}"
        eval_id = f"eval-{effective_run_id}"
        record = RunRecord(
            eval_id=eval_id,
            run_id=effective_run_id,
            project_id=self.project,
            model_version="baseline-nova-pro",
            eval_type="baseline",
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            pass_rate=metrics["pass_rate"],
            pass_rate_ci_low=metrics["pass_rate_ci_low"],
            pass_rate_ci_high=metrics["pass_rate_ci_high"],
            pass_rate_margin=metrics["pass_rate_margin"],
            rouge_l=metrics["rouge_l"],
            latency_p50=metrics["latency_p50"],
            latency_p95=metrics["latency_p95"],
            n_samples=nb,
            sample_size_sufficient=metrics["sample_size_sufficient"],
            evaluator_method="domain-aware",
            notes=f"mlflow_run_id={mlflow_run_id}" if mlflow_run_id else None,
        )
        try:
            _kpi_store.save_evaluation(record)
            logger.info("KPI baseline saved: run_id=%s", effective_run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("KPI save failed (non-blocking): %s", exc)

        return {"metrics": metrics, "run_id": effective_run_id}

    def run_test_set_by_category(
        self,
        model_path: str,
        test_jsonl: Path | str,
        run_id: str,
        port: int = 8080,
        model_version: str = "v1",
    ) -> dict[str, Any]:
        """Run evaluation broken down by category field in the JSONL.

        Requires pairs to have a 'category' field. Falls back to 'uncategorized'
        when the field is absent. Saves CategoryBreakdownItem per category to the
        KPI store and returns per-category metrics.
        """
        from collections import defaultdict  # noqa: PLC0415
        from pipeline.utils.kpi_store import CategoryBreakdownItem  # noqa: PLC0415

        domain = self.config.get("domain", "code")
        records = read_jsonl(test_jsonl)

        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in records:
            buckets[r.get("category", "uncategorized")].append(r)

        category_results: list[dict[str, Any]] = []
        breakdown_items: list[CategoryBreakdownItem] = []

        for category, cat_records in sorted(buckets.items()):
            pass_count = 0
            rouge_scores: list[float] = []

            for r in cat_records:
                completion, _ = _call_local_model(r["prompt"], model_path, port)
                reference = r.get("completion")
                eval_result = _evaluate_pair(completion, reference, domain)
                if eval_result.correct:
                    pass_count += 1
                rouge_scores.append(rouge_l_score(completion, reference) if reference else 0.0)

            n_cat = len(cat_records)
            ci = wilson_ci(pass_count, n_cat)
            rouge_mean = sum(rouge_scores) / len(rouge_scores) if rouge_scores else 0.0

            category_results.append({
                "category": category,
                "n": n_cat,
                "pass_count": pass_count,
                "pass_rate": round(pass_count / n_cat, 4) if n_cat else 0.0,
                "ci_low": ci.low,
                "ci_high": ci.high,
                "rouge_l": round(rouge_mean, 4),
            })

            breakdown_items.append(CategoryBreakdownItem(
                category=category,
                n=n_cat,
                pass_count=pass_count,
                pass_rate=round(pass_count / n_cat, 4) if n_cat else 0.0,
                ci_low=ci.low,
                ci_high=ci.high,
                rouge_l=round(rouge_mean, 4),
            ))

        eval_id = f"eval-{run_id}"
        try:
            _kpi_store.save_category_breakdown(
                eval_id=eval_id,
                run_id=run_id,
                project_id=self.project,
                items=breakdown_items,
            )
            logger.info("Category breakdown saved: %d categories, run_id=%s", len(breakdown_items), run_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Category breakdown save failed (non-blocking): %s", exc)

        return {"categories": category_results, "run_id": run_id}

    def run_perf_extended(
        self,
        model_path: str,
        run_id: str,
        port: int = 8080,
        n_warmup: int = 3,
        n_measure: int = 10,
    ) -> dict[str, Any]:
        """Measure extended performance metrics: tokens/s, adapter size, TTFT.

        Sends short standardized prompts to the local model server and measures:
        - tokens_per_second: output tokens / latency (steady-state, after warmup)
        - ttft_ms: time-to-first-token proxy (latency on a 1-token completion prompt)
        - adapter_size_mb: disk size of the adapter directory
        """
        import httpx  # noqa: PLC0415
        import os  # noqa: PLC0415

        _WARMUP_PROMPT = "Hello"
        _MEASURE_PROMPT = "Write a one-sentence summary of the Python language."
        _TTFT_PROMPT = "Yes"

        def _call_with_token_count(prompt: str, max_tokens: int = 200) -> tuple[float, int, float]:
            """Returns (latency_ms, output_tokens, tokens_per_second)."""
            t0 = time.perf_counter()
            try:
                resp = httpx.post(
                    f"http://localhost:{port}/v1/chat/completions",
                    json={
                        "model": "local",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": max_tokens,
                        "temperature": 0.0,
                    },
                    timeout=60.0,
                )
                latency = (time.perf_counter() - t0) * 1000
                body = resp.json()
                tokens = body.get("usage", {}).get("completion_tokens", 0)
                tps = (tokens / (latency / 1000)) if latency > 0 and tokens > 0 else 0.0
                return latency, tokens, tps
            except Exception as exc:  # noqa: BLE001
                logger.warning("Perf probe failed: %s", exc)
                return (time.perf_counter() - t0) * 1000, 0, 0.0

        # warmup — don't measure
        for _ in range(n_warmup):
            _call_with_token_count(_WARMUP_PROMPT, max_tokens=20)

        # steady-state tokens/s
        tps_values: list[float] = []
        for _ in range(n_measure):
            _, _, tps = _call_with_token_count(_MEASURE_PROMPT, max_tokens=200)
            if tps > 0:
                tps_values.append(tps)

        tokens_per_second = round(sum(tps_values) / len(tps_values), 2) if tps_values else 0.0

        # TTFT proxy — short max_tokens to capture first-token latency
        ttft_values: list[float] = []
        for _ in range(5):
            lat, _, _ = _call_with_token_count(_TTFT_PROMPT, max_tokens=1)
            ttft_values.append(lat)
        ttft_ms = round(sum(ttft_values) / len(ttft_values), 1) if ttft_values else 0.0

        # adapter size on disk
        adapter_size_mb = 0.0
        adapter_path = Path(model_path)
        if adapter_path.exists():
            total_bytes = sum(f.stat().st_size for f in adapter_path.rglob("*") if f.is_file())
            adapter_size_mb = round(total_bytes / (1024 * 1024), 2)

        metrics = {
            "tokens_per_second": tokens_per_second,
            "ttft_ms": ttft_ms,
            "adapter_size_mb": adapter_size_mb,
        }

        # Patch the existing eval record with perf metrics
        try:
            from pipeline.utils.kpi_store import KpiStore  # noqa: PLC0415
            store = KpiStore()
            store.patch_perf(run_id, metrics)
            logger.info("Perf metrics patched: run_id=%s tps=%.1f ttft=%.0fms adapter=%.1fMo",
                        run_id, tokens_per_second, ttft_ms, adapter_size_mb)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Perf patch failed (non-blocking): %s", exc)

        return {"perf": metrics, "run_id": run_id}

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
