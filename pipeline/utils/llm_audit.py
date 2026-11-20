"""Audit trail of LLM calls: the decision is recorded BEFORE the prompt is sent.

In the API process or a host script the ledger is written directly. When MLOPS_API_URL is set
(Celery worker container) the record goes through the token-protected API instead.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pipeline.utils import ledger_client

_log = logging.getLogger("mlops.llm_audit")


def _via_api() -> bool:
    return bool(os.environ.get("MLOPS_API_URL"))


def start(call: dict[str, Any]) -> bool:
    """Record the call decision. Returns False when it could not be recorded."""
    try:
        if _via_api():
            return ledger_client.report_llm_call(call)
        from pipeline.utils.run_ledger import get_ledger  # noqa: PLC0415

        get_ledger().record_llm_call(call)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("LLM audit record failed: %s", exc)
        return False


def finish(call_id: str, *, ok: bool, latency_ms: int, response_chars: int = 0, error: str | None = None) -> None:
    """Record the outcome (best-effort: the decision row already proves what was sent)."""
    try:
        if _via_api():
            ledger_client.report_llm_outcome(
                call_id, ok=ok, latency_ms=latency_ms, response_chars=response_chars, error=error
            )
        else:
            from pipeline.utils.run_ledger import get_ledger  # noqa: PLC0415

            get_ledger().finish_llm_call(
                call_id, ok=ok, latency_ms=latency_ms, response_chars=response_chars, error=error
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("LLM audit outcome failed: %s", exc)
