"""Projects CRUD — config stored in central SQLite, per-project DB created on init."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import (
    TASK_TYPES,
    DOMAINS,
    get_project_config,
    list_project_configs,
    save_project_config,
)
from api.db import ensure_project_db, execute, fetchall, fetchone

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    classification: str = "interne"
    domain: str = "code"
    task_type: str = "generation"
    model: str = "qwen2.5-coder-7b-4bit"
    model_format: str = "qwen"
    quality_thresholds: dict[str, Any] = {
        "min_pass_rate": 0.85,
        "min_rouge_l": 0.65,
        "max_latency_ratio": 2.0,
        "min_pairs": 50,
        "min_avg_quality": 0.6,
    }
    min_tokens: int = 50
    max_tokens: int = 2048
    blacklist_patterns: list[str] = ["password", "secret", "api_key", "TODO", "FIXME"]
    retrain_threshold: dict[str, Any] = {
        "new_patterns": 200,
        "min_quality": 0.85,
    }
    adapters: list[dict[str, Any]] = []
    # Advisor-generated fields — persisted as-is in the YAML
    advisor_need: str = ""
    advisor_notes: str = ""
    rag_global: bool = False
    transform: dict[str, Any] = {}


class ProjectPatch(BaseModel):
    description: str | None = None
    model: str | None = None
    model_format: str | None = None
    quality_thresholds: dict[str, Any] | None = None
    min_tokens: int | None = None
    max_tokens: int | None = None
    blacklist_patterns: list[str] | None = None
    adapters: list[dict[str, Any]] | None = None


def _enrich(project_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge YAML config with SQLite stats."""
    row = fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    stats = fetchone(
        """SELECT COUNT(*) as n_runs,
                  MAX(started_at) as last_run,
                  (SELECT status FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 1) as last_status
           FROM runs WHERE project_id = ?""",
        (project_id, project_id),
    )
    return {
        **cfg,
        "db": row,
        "n_runs": (stats or {}).get("n_runs", 0),
        "last_run": (stats or {}).get("last_run"),
        "last_status": (stats or {}).get("last_status"),
    }


@router.get("")
def list_projects():
    configs = list_project_configs()
    return [_enrich(c["id"], c) for c in configs]


@router.get("/{project_id}")
def get_project(project_id: str):
    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return _enrich(project_id, cfg)


@router.post("", status_code=201)
def create_project(body: ProjectCreate):
    if body.domain not in DOMAINS:
        raise HTTPException(422, f"Unsupported domain: {body.domain}")
    if body.task_type not in TASK_TYPES:
        raise HTTPException(422, f"Unsupported task_type: {body.task_type}")

    project_id = body.name.lower().replace(" ", "-").replace("_", "-")
    if get_project_config(project_id):
        raise HTTPException(409, f"Project '{project_id}' already exists")

    cfg = {
        "name":        body.name,
        "description": body.description,
        "classification": body.classification,
        "domain":      body.domain,
        "task_type":   body.task_type,
        "model":       body.model,
        "model_format": body.model_format,
        "rag_collection": f"patterns_{project_id}",
        "baseline":    {"provider": "nova-pro", "model_id": "us.amazon.nova-pro-v1:0", "measured": False, "metrics": {}},
        "quality_thresholds": body.quality_thresholds,
        "min_tokens":  body.min_tokens,
        "max_tokens":  body.max_tokens,
        "blacklist_patterns": body.blacklist_patterns,
        "retrain_threshold": body.retrain_threshold,
        "data": {
            "raw":               f"datalake/projects/{project_id}/raw/",
            "processed":         f"datalake/projects/{project_id}/processed/",
            "fine_tuning_ready": f"datalake/projects/{project_id}/fine-tuning-ready/",
        },
        "adapters": body.adapters,
    }

    # Advisor context — only written when provided
    if body.advisor_need:
        cfg["advisor_need"] = body.advisor_need
    if body.advisor_notes:
        cfg["advisor_notes"] = body.advisor_notes
    if body.rag_global:
        cfg["rag_global"] = body.rag_global
    if body.transform:
        cfg["transform"] = body.transform
    if body.classification in ("confidentiel", "secret"):
        cfg["isolation_level"] = "sovereign"

    save_project_config(project_id, cfg)

    # Per-project isolated SQLite DB
    ensure_project_db(project_id)

    # Create datalake directories
    from pathlib import Path
    for stage in ("raw", "processed", "fine-tuning-ready"):
        Path(f"datalake/projects/{project_id}/{stage}").mkdir(parents=True, exist_ok=True)
    Path(f"datalake/projects/{project_id}/raw/feedback").mkdir(parents=True, exist_ok=True)

    return {**cfg, "id": project_id}


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str):
    from pathlib import Path
    import shutil

    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")

    # Remove SQLite rows in dependency order
    execute("DELETE FROM eval_category_breakdown WHERE project_id = ?", (project_id,))
    execute("DELETE FROM eval_learning_curve WHERE project_id = ?", (project_id,))
    execute("DELETE FROM eval_robustness WHERE project_id = ?", (project_id,))
    execute("DELETE FROM eval_security WHERE project_id = ?", (project_id,))
    execute("DELETE FROM eval_compliance WHERE project_id = ?", (project_id,))
    execute("DELETE FROM evaluations WHERE project_id = ?", (project_id,))
    execute("DELETE FROM models WHERE project_id = ?", (project_id,))
    execute("DELETE FROM adapter_data_links WHERE adapter_id IN (SELECT id FROM adapters WHERE project_id = ?)", (project_id,))
    execute("DELETE FROM adapters WHERE project_id = ?", (project_id,))
    execute("DELETE FROM datasets WHERE project_id = ?", (project_id,))
    execute("DELETE FROM rag_feedback WHERE project_id = ?", (project_id,))
    execute("DELETE FROM production_feedback WHERE project = ?", (project_id,))
    # Disable FK checks for this batch — MLflow shares the DB and has cross-table FKs
    # that don't involve our rows; re-enabled automatically on connection close.
    from api.db import get_db  # noqa: PLC0415
    with get_db() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM runs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.execute("PRAGMA foreign_keys=ON")

    # Remove datalake directory (non-blocking — log on error)
    datalake_dir = Path("datalake/projects") / project_id
    if datalake_dir.exists():
        shutil.rmtree(datalake_dir, ignore_errors=True)

    # Drop Qdrant collection (non-blocking)
    try:
        from qdrant_client import QdrantClient  # noqa: PLC0415
        client = QdrantClient(host="localhost", port=6333)
        client.delete_collection(f"patterns_{project_id}")
    except Exception:
        pass


@router.patch("/{project_id}")
def patch_project(project_id: str, body: ProjectPatch):
    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")

    patch = body.model_dump(exclude_none=True)
    cfg.update(patch)
    save_project_config(project_id, cfg)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))

    return _enrich(project_id, cfg)
