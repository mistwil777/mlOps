"""Compliance KPI collector.

Measures:
- Format compliance: % of outputs matching the declared output schema
- PII detection: % of inputs where PII was correctly handled (not echoed)
- Audit traceability: completeness of run metadata (model version, dataset, timestamp)

Deterministic — no LLM call. Derived from completions + project config.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pipeline.utils.kpi_store import ComplianceMetrics
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.kpi.compliance")


def _check_json_schema(output: str, schema: dict[str, Any]) -> bool:
    """Returns True if output is valid JSON matching required keys."""
    try:
        parsed = json.loads(output.strip())
    except json.JSONDecodeError:
        # Try to extract JSON from output (model may add prose around it)
        match = re.search(r"\{.*\}", output, re.DOTALL)
        if not match:
            return False
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return False

    required = schema.get("required", [])
    return all(k in parsed for k in required)


def _check_code_format(output: str, lang: str) -> bool:
    """Minimal structural check for code outputs."""
    output = output.strip()
    if lang == "java":
        # Must contain at least one class or method declaration
        return bool(re.search(r"\bpublic\s+(class|void|static|interface)", output))
    if lang in ("python", "py"):
        return bool(re.search(r"\bdef\s+\w+|class\s+\w+", output))
    # Generic: non-empty
    return len(output) > 10


def _check_format(output: str, format_spec: dict[str, Any]) -> bool:
    """Dispatch to the right format checker based on format_spec."""
    fmt_type = format_spec.get("type", "text")
    if fmt_type == "json":
        return _check_json_schema(output, format_spec)
    if fmt_type == "code":
        return _check_code_format(output, format_spec.get("lang", ""))
    if fmt_type == "regex":
        pattern = format_spec.get("pattern", "")
        return bool(re.search(pattern, output, re.DOTALL)) if pattern else True
    return True  # "text" — no structural constraint


def collect_compliance(
    completions: list[dict[str, str]],
    project_config: dict[str, Any],
    eval_id: str,
    run_id: str,
) -> ComplianceMetrics:
    """Collect compliance metrics from a list of model completions.

    Args:
        completions: list of {"hypothesis": ..., "prompt": ...} dicts
        project_config: full project YAML config dict
        eval_id: for traceability score lookup
        run_id: for traceability score lookup
    """
    sector = project_config.get("sector", "generic")
    format_spec = project_config.get("output_format", {})
    n = len(completions)

    # ── Format compliance ──────────────────────────────────────────────────────
    if format_spec and n > 0:
        compliant = sum(
            1 for c in completions
            if _check_format(c.get("hypothesis", ""), format_spec)
        )
        format_compliance_rate: float | None = round(compliant / n, 4)
    else:
        # Infer from domain + task_type if no explicit format_spec
        domain = project_config.get("domain", "")
        lang = project_config.get("transform", {}).get("params", {}).get("lang", "")
        if domain == "code" and lang:
            compliant = sum(
                1 for c in completions
                if _check_code_format(c.get("hypothesis", ""), lang)
            )
            format_compliance_rate = round(compliant / n, 4) if n else None
        else:
            format_compliance_rate = None
        n_checks = n
    n_checks = n if format_compliance_rate is not None else 0

    # ── PII detection rate ─────────────────────────────────────────────────────
    # Check that PII in the prompt was NOT echoed verbatim in the output
    _PII = re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"  # email
        r"|\b(?:\+33|0)[1-9](?:[\s.-]?\d{2}){4}\b"                # FR phone
        r"|\bFR\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{2,3}\b"  # IBAN
    )
    pii_items = [c for c in completions if _PII.search(c.get("prompt", ""))]
    if pii_items:
        pii_not_leaked = sum(
            1 for c in pii_items
            if not _PII.search(c.get("hypothesis", ""))
        )
        pii_detection_rate: float | None = round(pii_not_leaked / len(pii_items), 4)
    else:
        pii_detection_rate = None  # no PII in prompts — metric not applicable

    # ── Audit traceability ─────────────────────────────────────────────────────
    try:
        from pipeline.utils.kpi_store import KpiStore  # noqa: PLC0415
        score = KpiStore().compute_audit_traceability_score(run_id, eval_id)
        audit_traceability_score: float | None = score
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit traceability score unavailable: %s", exc)
        audit_traceability_score = None

    return ComplianceMetrics(
        sector=sector,
        format_compliance_rate=format_compliance_rate,
        regulatory_citation_accuracy=None,  # requires domain-specific judge — future extension
        pii_detection_rate=pii_detection_rate,
        audit_traceability_score=audit_traceability_score,
        n_format_checks=n_checks,
    )
