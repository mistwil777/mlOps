"""Source analysis endpoints — category taxonomy proposal and validation gate."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/analyze", tags=["analyze"])

_PROJECTS_DIR = Path("config/projects")


def _project_cfg(project_id: str) -> dict[str, Any]:
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Project config not found: {project_id}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _report_path(project_id: str) -> Path:
    cfg = _project_cfg(project_id)
    return Path(cfg["data"]["raw"]).parent / "analysis_report.json"


# ── GET report ────────────────────────────────────────────────────────────────

@router.get("/{project_id}/report")
async def get_report(project_id: str) -> dict[str, Any]:
    path = _report_path(project_id)
    if not path.exists():
        return {"project_id": project_id, "validation_status": "not_run", "proposed_categories": [], "missing_from_config": []}
    return json.loads(path.read_text())


# ── POST run analysis ──────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    sample_size: int = 20

_RUNNING: set[str] = set()

@router.post("/{project_id}/run")
async def run_analysis(project_id: str, body: RunRequest, background_tasks: BackgroundTasks) -> dict[str, str]:
    if project_id in _RUNNING:
        return {"status": "already_running"}
    _project_cfg(project_id)  # validate project exists
    _RUNNING.add(project_id)
    background_tasks.add_task(_run_bg, project_id, body.sample_size)
    return {"status": "started"}


async def _run_bg(project_id: str, sample_size: int) -> None:
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_sync, project_id, sample_size)
    finally:
        _RUNNING.discard(project_id)


def _run_sync(project_id: str, sample_size: int) -> None:
    from pipeline.analyze_source import analyze  # noqa: PLC0415
    analyze(project_id, sample_size)


# ── POST validate ──────────────────────────────────────────────────────────────

class ValidateRequest(BaseModel):
    accepted_categories: list[dict[str, Any]]
    rejected_names: list[str] = []
    validator: str = "human"


@router.post("/{project_id}/validate")
async def validate_report(project_id: str, body: ValidateRequest) -> dict[str, Any]:
    path = _report_path(project_id)
    if not path.exists():
        raise HTTPException(404, "No analysis report found — run analysis first")

    report = json.loads(path.read_text())
    report["validation_status"] = "validated"
    report["validated_by"] = body.validator
    report["validated_at"] = datetime.now(timezone.utc).isoformat()
    report["accepted_categories"] = body.accepted_categories
    report["rejected_names"] = body.rejected_names
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # Write accepted categories back into project YAML
    _apply_categories_to_config(project_id, body.accepted_categories)

    return {"status": "validated", "categories_written": len(body.accepted_categories)}


def _apply_categories_to_config(project_id: str, categories: list[dict[str, Any]]) -> None:
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    with path.open() as fh:
        cfg = yaml.safe_load(fh) or {}

    cfg["categories"] = [
        {"name": c["name"], "description": c.get("description", "")}
        for c in categories
    ]

    with path.open("w") as fh:
        yaml.dump(cfg, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)


# ── GET gate status ────────────────────────────────────────────────────────────

@router.get("/{project_id}/gate")
async def get_gate(project_id: str) -> dict[str, Any]:
    """Returns whether transform is allowed for this project."""
    path = _report_path(project_id)
    if not path.exists():
        return {"can_transform": False, "reason": "analysis_not_run"}
    report = json.loads(path.read_text())
    status = report.get("validation_status", "pending")
    if status == "validated":
        return {"can_transform": True, "reason": "validated", "categories": report.get("accepted_categories", [])}
    if status == "pending":
        return {"can_transform": False, "reason": "pending_validation"}
    return {"can_transform": False, "reason": status}
