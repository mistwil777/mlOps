"""Worker-side reporting to the job ledger: the API stays the single writer of the SQLite file.

Celery workers (docker) never open the ledger database; they POST their events to the API.
Reporting is best-effort: it can never make a task fail, and it is off unless both
MLOPS_API_URL and MLOPS_INTERNAL_TOKEN are set.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

_log = logging.getLogger("mlops.ledger_client")
_TIMEOUT_S = 3.0
_MAX_RESULT_BYTES = 20_000


def report_job(
    job_id: str | None,
    *,
    lines: list[str] | None = None,
    status: str | None = None,
    returncode: int | None = None,
    result: dict[str, Any] | None = None,
    level: str = "info",
) -> bool:
    """Send log lines and/or a status change for a job. Returns True when the API accepted it."""
    base = os.environ.get("MLOPS_API_URL", "")
    token = os.environ.get("MLOPS_INTERNAL_TOKEN", "")
    if not (job_id and base and token):
        return False
    payload = {"lines": lines or [], "status": status, "returncode": returncode, "result": result, "level": level}
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/jobs/{job_id}/report",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S):
            return True
    except Exception as exc:  # noqa: BLE001 - reporting must never break a task
        _log.warning("job report failed for %s: %s", job_id, exc)
        return False


def final_status(state: str | None, retval: Any) -> str:
    """Ledger status of a finished Celery task: a task that returns a non-zero returncode failed."""
    if state != "SUCCESS":
        return "failed"
    if isinstance(retval, dict) and retval.get("returncode") not in (None, 0):
        return "failed"
    return "completed"


def result_for_report(retval: Any) -> dict[str, Any] | None:
    """A small JSON-safe result worth storing (never the raw output lines)."""
    if not isinstance(retval, dict):
        return None
    slim = {k: v for k, v in retval.items() if k != "lines"}
    try:
        return slim if slim and len(json.dumps(slim)) <= _MAX_RESULT_BYTES else None
    except (TypeError, ValueError):
        return None


class LineBatcher:
    """Buffer log lines and send them in batches (every `size` lines or `interval_s` seconds)."""

    def __init__(self, job_id: str | None, size: int = 20, interval_s: float = 1.0) -> None:
        self.job_id = job_id
        self.size = size
        self.interval_s = interval_s
        self._buffer: list[str] = []
        self._last = time.monotonic()

    def add(self, line: str) -> None:
        self._buffer.append(line)
        if len(self._buffer) >= self.size or time.monotonic() - self._last >= self.interval_s:
            self.flush()

    def flush(self) -> None:
        if self._buffer:
            report_job(self.job_id, lines=self._buffer)
            self._buffer = []
        self._last = time.monotonic()


def _post(path: str, payload: dict[str, Any]) -> bool:
    base = os.environ.get("MLOPS_API_URL", "")
    token = os.environ.get("MLOPS_INTERNAL_TOKEN", "")
    if not (base and token):
        return False
    request = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Internal-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S):
            return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("report to %s failed: %s", path, exc)
        return False


def report_llm_call(call: dict[str, Any]) -> bool:
    """Record an LLM call decision through the API (workers never open the ledger file)."""
    return _post("/api/llm-calls", call)


def report_llm_outcome(call_id: str, **outcome: Any) -> bool:
    return _post(f"/api/llm-calls/{call_id}/outcome", outcome)
