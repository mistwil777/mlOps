"""KPI dashboard endpoints — multi-project overview, per-project history, run detail."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.db import execute, fetchall, fetchone
from pipeline.utils.kpi_store import (
    KpiStore,
    RunRecord,
    CategoryBreakdownItem,
    RobustnessMetrics,
    SecurityMetrics,
    ComplianceMetrics,
)

router = APIRouter(prefix="/api/kpis", tags=["kpis"])
_store = KpiStore()

_ACTIVE_JOBS: dict[str, str] = {}  # job_id → status


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Overview ──────────────────────────────────────────────────────────────────

@router.get("/dashboard")
async def get_dashboard(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """One row per project: latest KPIs + delta vs previous version."""
    summaries = _store.get_all_projects_summary()[:limit]
    result = []
    for s in summaries:
        d = asdict(s)
        d["quality_gate_pass"] = (s.pass_rate >= 0.7) if s.pass_rate is not None else None
        result.append(d)
    return result


# ── Project KPIs ──────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}")
async def get_project_kpis(project_id: str) -> dict[str, Any]:
    """Latest eval + KPI history for a project."""
    project = fetchone("SELECT id, name FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, f"Project {project_id!r} not found")

    latest = _store.get_latest_eval(project_id)
    history = _store.get_kpi_history(project_id, limit=20)

    return {
        "project_id": project_id,
        "project_name": project["name"],
        "latest_eval": asdict(latest) if latest else None,
        "history": [asdict(h) for h in history],
    }


@router.get("/projects/{project_id}/history")
async def get_project_kpi_history(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Chronological KPI history for charts."""
    history = _store.get_kpi_history(project_id, limit=limit)
    return [asdict(h) for h in reversed(history)]  # oldest first for charts


@router.get("/projects/{project_id}/learning-curve")
async def get_learning_curve(project_id: str) -> list[dict[str, Any]]:
    """Pass rate as a function of training pair count."""
    return _store.get_learning_curve(project_id)


@router.get("/projects/{project_id}/compare")
async def compare_runs(
    project_id: str,
    run_ids: str = Query(..., description="Comma-separated run IDs"),
) -> list[dict[str, Any]]:
    """Side-by-side comparison of multiple runs."""
    ids = [r.strip() for r in run_ids.split(",") if r.strip()]
    result = []
    for run_id in ids:
        evals = fetchall(
            "SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        )
        if evals:
            row = dict(evals[0])
            row["robustness"] = _store.get_robustness(row["id"])
            row["security"] = _store.get_security(row["id"])
            row["compliance"] = _store.get_compliance(row["id"])
            row["category_breakdown"] = _store.get_category_breakdown(row["id"])
            result.append(row)
    return result


# ── Run detail ────────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/full")
async def get_run_full_kpis(run_id: str) -> dict[str, Any]:
    """All 6 KPI dimensions for a single run."""
    eval_row = fetchone(
        "SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    )
    if not eval_row:
        raise HTTPException(404, f"No evaluation found for run {run_id!r}")

    eval_id = eval_row["id"]
    return {
        "run_id": run_id,
        "eval_id": eval_id,
        "functional": dict(eval_row),
        "robustness": _store.get_robustness(eval_id),
        "security": _store.get_security(eval_id),
        "compliance": _store.get_compliance(eval_id),
        "category_breakdown": _store.get_category_breakdown(eval_id),
    }


@router.get("/runs/{run_id}/category-breakdown")
async def get_category_breakdown(run_id: str) -> list[dict[str, Any]]:
    eval_row = fetchone(
        "SELECT id FROM evaluations WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    )
    if not eval_row:
        raise HTTPException(404, f"No evaluation found for run {run_id!r}")
    return _store.get_category_breakdown(eval_row["id"])


# ── Advanced eval launchers ───────────────────────────────────────────────────

class RobustnessEvalRequest(BaseModel):
    n_consistency_prompts: int = 10
    temperatures: list[float] = [0.0, 0.3, 0.7]
    test_jsonl: str | None = None


class SecurityEvalRequest(BaseModel):
    n_probes: int = 20
    sector: str = "generic"


class ComplianceEvalRequest(BaseModel):
    sector: str = "generic"
    test_jsonl: str | None = None


@router.post("/runs/{run_id}/robustness-eval")
async def launch_robustness_eval(run_id: str, body: RobustnessEvalRequest) -> dict[str, Any]:
    run = fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(404, f"Run {run_id!r} not found")
    if run["status"] != "completed":
        raise HTTPException(400, "Run must be completed before running robustness eval")

    job_id = str(uuid.uuid4())
    _ACTIVE_JOBS[job_id] = "pending"

    asyncio.create_task(_run_robustness_eval(job_id, run_id, dict(run), body))
    return {"job_id": job_id, "status": "pending"}


@router.post("/runs/{run_id}/security-eval")
async def launch_security_eval(run_id: str, body: SecurityEvalRequest) -> dict[str, Any]:
    run = fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(404, f"Run {run_id!r} not found")

    job_id = str(uuid.uuid4())
    _ACTIVE_JOBS[job_id] = "pending"
    asyncio.create_task(_run_security_eval(job_id, run_id, dict(run), body))
    return {"job_id": job_id, "status": "pending"}


@router.post("/runs/{run_id}/compliance-eval")
async def launch_compliance_eval(run_id: str, body: ComplianceEvalRequest) -> dict[str, Any]:
    run = fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(404, f"Run {run_id!r} not found")

    job_id = str(uuid.uuid4())
    _ACTIVE_JOBS[job_id] = "pending"
    asyncio.create_task(_run_compliance_eval(job_id, run_id, dict(run), body))
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict[str, str]:
    status = _ACTIVE_JOBS.get(job_id)
    if status is None:
        raise HTTPException(404, f"Job {job_id!r} not found")
    return {"job_id": job_id, "status": status}


# ── Background tasks ──────────────────────────────────────────────────────────

async def _get_or_create_eval_id(run_id: str) -> str:
    """Return latest eval_id for this run, or create a placeholder evaluation."""
    row = fetchone(
        "SELECT id FROM evaluations WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
        (run_id,),
    )
    if row:
        return row["id"]
    # Create minimal eval record as anchor
    eval_id = str(uuid.uuid4())
    run = fetchone("SELECT project_id FROM runs WHERE id = ?", (run_id,))
    execute(
        "INSERT INTO evaluations (id, run_id, project_id, eval_type) VALUES (?, ?, ?, ?)",
        (eval_id, run_id, run["project_id"] if run else "", "advanced"),
    )
    return eval_id


async def _run_robustness_eval(
    job_id: str, run_id: str, run: dict[str, Any], body: RobustnessEvalRequest
) -> None:
    _ACTIVE_JOBS[job_id] = "running"
    try:
        from pipeline.evaluate_robustness import RobustnessEvaluator  # noqa: PLC0415

        evaluator = RobustnessEvaluator(run["project_id"])
        metrics = evaluator.full_eval(
            run_id=run_id,
            model_path=run.get("adapter_path", ""),
            n_consistency_prompts=body.n_consistency_prompts,
            temperatures=body.temperatures,
        )
        eval_id = await _get_or_create_eval_id(run_id)
        _store.save_robustness(
            eval_id, run_id, run["project_id"],
            RobustnessMetrics(**{k: metrics.get(k) for k in RobustnessMetrics.__dataclass_fields__}),
        )
        _ACTIVE_JOBS[job_id] = "completed"
    except Exception as exc:  # noqa: BLE001
        _ACTIVE_JOBS[job_id] = f"failed: {exc}"


async def _run_security_eval(
    job_id: str, run_id: str, run: dict[str, Any], body: SecurityEvalRequest
) -> None:
    _ACTIVE_JOBS[job_id] = "running"
    try:
        from pipeline.evaluate_security import SecurityEvaluator  # noqa: PLC0415

        evaluator = SecurityEvaluator(run["project_id"], sector=body.sector)
        metrics = evaluator.full_eval(
            run_id=run_id,
            model_path=run.get("adapter_path", ""),
            n_probes=body.n_probes,
        )
        eval_id = await _get_or_create_eval_id(run_id)
        _store.save_security(
            eval_id, run_id, run["project_id"],
            SecurityMetrics(**{k: metrics.get(k) for k in SecurityMetrics.__dataclass_fields__}),
        )
        _ACTIVE_JOBS[job_id] = "completed"
    except Exception as exc:  # noqa: BLE001
        _ACTIVE_JOBS[job_id] = f"failed: {exc}"


async def _run_compliance_eval(
    job_id: str, run_id: str, run: dict[str, Any], body: ComplianceEvalRequest
) -> None:
    _ACTIVE_JOBS[job_id] = "running"
    try:
        from pipeline.evaluate_compliance import ComplianceEvaluator  # noqa: PLC0415

        evaluator = ComplianceEvaluator(run["project_id"], sector=body.sector)
        metrics = evaluator.full_eval(run_id=run_id, model_path=run.get("adapter_path", ""))
        eval_id = await _get_or_create_eval_id(run_id)
        _store.save_compliance(
            eval_id, run_id, run["project_id"],
            ComplianceMetrics(**{k: metrics.get(k) for k in ComplianceMetrics.__dataclass_fields__}),
        )
        _ACTIVE_JOBS[job_id] = "completed"
    except Exception as exc:  # noqa: BLE001
        _ACTIVE_JOBS[job_id] = f"failed: {exc}"
