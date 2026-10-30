"""QA test run tracking — REST endpoints."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

# qa_store lives in tests/qa/ — add to path at import time
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests" / "qa"))
from qa_store import (  # noqa: E402
    CheckResult,
    RunPayload,
    get_failure_analysis,
    get_run_detail,
    get_runs,
    get_trend,
    save_run,
)

router = APIRouter(prefix="/api/qa", tags=["qa"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class CheckIn(BaseModel):
    scenario_id: str
    check_index: int
    label: str
    status: str          # "pass" | "fail" | "skip"
    notes: str = ""


class RunIn(BaseModel):
    cdc_id: str
    cdc_version: str = ""
    tester: str = ""
    notes: str = ""
    duration_s: Optional[float] = None
    checks: list[CheckIn]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/run", status_code=201)
def create_run(body: RunIn) -> dict[str, Any]:
    """Record a completed test session."""
    if not body.checks:
        raise HTTPException(422, "checks list is empty")
    invalid = [c.status for c in body.checks if c.status not in ("pass", "fail", "skip")]
    if invalid:
        raise HTTPException(422, f"Invalid status values: {set(invalid)}")

    payload = RunPayload(
        cdc_id=body.cdc_id,
        cdc_version=body.cdc_version,
        tester=body.tester,
        notes=body.notes,
        duration_s=body.duration_s,
        checks=[
            CheckResult(
                scenario_id=c.scenario_id,
                check_index=c.check_index,
                label=c.label,
                status=c.status,
                notes=c.notes,
            )
            for c in body.checks
        ],
    )
    run_id = save_run(payload)
    return {"run_id": run_id, "ok": True}


@router.get("/runs")
def list_runs(cdc_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    """List recent test runs, optionally filtered by CDC."""
    return get_runs(cdc_id=cdc_id, limit=limit)


@router.get("/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    """Full detail of a test run including individual check results."""
    detail = get_run_detail(run_id)
    if not detail:
        raise HTTPException(404, f"Run {run_id} not found")
    return detail


@router.get("/analysis")
def failure_analysis(cdc_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Checks ranked by failure rate — identifies systematic weaknesses."""
    return get_failure_analysis(cdc_id=cdc_id)


@router.get("/trend/{cdc_id}")
def trend(cdc_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Pass rate over time for a CDC — detects regression across versions."""
    return get_trend(cdc_id=cdc_id, limit=limit)
