"""Job history (read) and worker reports (write, internal) on top of the durable ledger."""

from __future__ import annotations

import os
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from pipeline.utils.ledger_client import final_status
from pipeline.utils.run_ledger import get_ledger

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

_WORKER_STATUSES = {"running", "completed", "failed"}


class JobReport(BaseModel):
    lines: list[str] = []
    status: Optional[str] = None
    returncode: Optional[int] = None
    result: Optional[dict[str, Any]] = None
    level: str = "info"


def _check_token(token: str | None) -> None:
    expected = os.environ.get("MLOPS_INTERNAL_TOKEN", "")
    if not expected:
        raise HTTPException(503, "Job reporting is disabled (MLOPS_INTERNAL_TOKEN is not set)")
    if not secrets.compare_digest(token or "", expected):
        raise HTTPException(403, "Invalid token")


def _reconcile(job: dict[str, Any]) -> dict[str, Any]:
    """A Celery job whose worker could not report (token unset, worker crash) is aligned with Celery's state."""
    if job["executor"] != "celery" or job["status"] not in ("pending", "running"):
        return job
    try:
        from api.celery_app import celery_app  # noqa: PLC0415

        result = celery_app.AsyncResult(job["id"])
        state = result.state
        if state == "REVOKED":
            new_status = "cancelled"
        elif state in ("SUCCESS", "FAILURE"):
            new_status = final_status(state, result.result if state == "SUCCESS" else None)
        elif state in ("STARTED", "PROGRESS"):
            new_status = "running"
        else:
            return job  # PENDING: queued, or unknown to Celery
    except Exception:  # noqa: BLE001 - broker unreachable: keep what the ledger knows
        return job
    ledger = get_ledger()
    ledger.set_status(job["id"], new_status)
    return ledger.get_job(job["id"]) or job


@router.get("")
def list_jobs(project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Job history, newest first (no log lines)."""
    jobs = get_ledger().list_jobs(project_id, min(max(limit, 1), 500))
    open_celery = 0
    for i, job in enumerate(jobs):
        if job["executor"] == "celery" and job["status"] in ("pending", "running") and open_celery < 20:
            open_celery += 1
            jobs[i] = _reconcile(job)
    return jobs


@router.get("/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = get_ledger().get_job(job_id)
    if job is None:
        raise HTTPException(404, f"Job {job_id!r} not found")
    return _reconcile(job)


@router.get("/{job_id}/events")
def job_events(job_id: str, since: int = 0) -> dict[str, Any]:
    """Log lines after event `since`, so a viewer can resume where it stopped."""
    ledger = get_ledger()
    if not ledger.exists(job_id):
        raise HTTPException(404, f"Job {job_id!r} not found")
    return {"job_id": job_id, "events": [{"seq": s, "line": m} for s, m in ledger.events_since(job_id, since)]}


@router.post("/{job_id}/report")
def report_job(
    job_id: str,
    body: JobReport,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, bool]:
    """Internal: a worker reports log lines, results and status changes (lines first, status last)."""
    _check_token(x_internal_token)
    if body.status is not None and body.status not in _WORKER_STATUSES:
        raise HTTPException(422, f"status must be one of {sorted(_WORKER_STATUSES)}")
    ledger = get_ledger()
    if not ledger.exists(job_id):
        raise HTTPException(404, f"Job {job_id!r} not found")
    if body.lines:
        ledger.log_many(job_id, body.lines, body.level)
    ledger.set_fields(job_id, returncode=body.returncode, result=body.result)
    if body.status:
        ledger.set_status(job_id, body.status)
    return {"ok": True}
