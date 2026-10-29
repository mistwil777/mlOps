"""Robustness KPI collector.

Measures:
- Temperature consistency: same prompt × N at temp 0/0.3/0.7 → pass rate per temp
- Output consistency: same prompt × N at temp 0 → % identical outputs
- Catastrophic forgetting: pass rate on a general benchmark after fine-tuning

All measurements use the local model server (mlx_lm.server).
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Callable

from pipeline.utils.kpi_store import RobustnessMetrics
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.kpi.robustness")

ModelFn = Callable[[str, float], str]  # (prompt, temperature) -> completion


def _call_model(prompt: str, temperature: float, port: int) -> str:
    import httpx  # noqa: PLC0415

    try:
        resp = httpx.post(
            f"http://localhost:{port}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
                "temperature": temperature,
            },
            timeout=60.0,
        )
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Model call failed (temp=%.1f): %s", temperature, exc)
        return ""


def _evaluate_completion(completion: str, reference: str, domain: str) -> bool:
    from pipeline.utils.evaluators import evaluate  # noqa: PLC0415
    return evaluate(completion, reference, domain).correct


def collect_robustness(
    prompts_with_refs: list[dict[str, str]],
    domain: str,
    port: int = 8080,
    n_consistency: int = 5,
) -> RobustnessMetrics:
    """Collect robustness metrics for a set of prompts.

    Args:
        prompts_with_refs: list of {"prompt": ..., "completion": ...} dicts
        domain: "code" | "text" | "json" — used by the evaluator
        port: local model server port
        n_consistency: number of repetitions for consistency measurement
    """
    if not prompts_with_refs:
        return RobustnessMetrics()

    # ── Temperature sweep ──────────────────────────────────────────────────────
    temps = [0.0, 0.3, 0.7]
    pass_by_temp: dict[float, int] = {t: 0 for t in temps}
    n_temp = len(prompts_with_refs)

    for item in prompts_with_refs:
        prompt = item["prompt"]
        ref = item.get("completion", "")
        for temp in temps:
            completion = _call_model(prompt, temp, port)
            if _evaluate_completion(completion, ref, domain):
                pass_by_temp[temp] += 1

    pr_temp0   = pass_by_temp[0.0] / n_temp if n_temp else None
    pr_temp03  = pass_by_temp[0.3] / n_temp if n_temp else None
    pr_temp07  = pass_by_temp[0.7] / n_temp if n_temp else None

    # degradation = drop from temp0 to temp07
    degradation = (pr_temp0 - pr_temp07) if pr_temp0 is not None and pr_temp07 is not None else None

    # ── Consistency (same prompt × n at temp 0) ────────────────────────────────
    # Use a subset — first min(5, len) prompts
    consistency_subset = prompts_with_refs[:min(5, len(prompts_with_refs))]
    consistency_scores: list[float] = []

    for item in consistency_subset:
        prompt = item["prompt"]
        outputs = [_call_model(prompt, 0.0, port) for _ in range(n_consistency)]
        most_common_count = Counter(outputs).most_common(1)[0][1]
        consistency_scores.append(most_common_count / n_consistency)

    consistency_score = sum(consistency_scores) / len(consistency_scores) if consistency_scores else None

    return RobustnessMetrics(
        pass_rate_temp0=round(pr_temp0, 4) if pr_temp0 is not None else None,
        pass_rate_temp03=round(pr_temp03, 4) if pr_temp03 is not None else None,
        pass_rate_temp07=round(pr_temp07, 4) if pr_temp07 is not None else None,
        consistency_score=round(consistency_score, 4) if consistency_score is not None else None,
        degradation_rate=round(degradation, 4) if degradation is not None else None,
        catastrophic_forgetting=None,  # requires external benchmark — optional extension
        n_consistency_prompts=len(consistency_subset) * n_consistency,
        n_temp_sweep_prompts=n_temp * len(temps),
    )
