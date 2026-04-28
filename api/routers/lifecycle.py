"""Lifecycle router — sol ↔ embedded cycle status per project."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import get_project_config
from api.db import execute, fetchall, fetchone
from pipeline.export_embedded import export_bundle
from pipeline.feedback import ingest_feedback_dir

router = APIRouter(prefix="/api/projects", tags=["lifecycle"])


def _rag_pattern_count(project_id: str) -> int:
    try:
        import lancedb  # noqa: PLC0415
        db = lancedb.connect(str(Path(f"lancedb/{project_id}")))
        tbl = db.open_table("patterns")
        return tbl.count_rows()
    except Exception:
        return 0


def _last_training_run(project_id: str) -> dict[str, Any] | None:
    return fetchone(
        """SELECT * FROM runs WHERE project_id = ? AND status = 'completed'
           ORDER BY finished_at DESC LIMIT 1""",
        (project_id,),
    )


def _last_embedded_export(project_id: str) -> dict[str, Any] | None:
    export_dir = Path("exports/embedded")
    if not export_dir.exists():
        return None
    bundles = sorted(
        [d for d in export_dir.iterdir() if d.is_dir() and d.name.startswith(project_id)],
        reverse=True,
    )
    if not bundles:
        return None
    manifest_path = bundles[0] / "manifest.json"
    if not manifest_path.exists():
        return None
    import json  # noqa: PLC0415
    with manifest_path.open() as fh:
        return json.load(fh)


def _pending_retrain(project_id: str) -> bool:
    row = fetchone(
        "SELECT id FROM runs WHERE project_id = ? AND status = 'pending_retrain' LIMIT 1",
        (project_id,),
    )
    return row is not None


@router.get("/{project_id}/lifecycle")
def get_lifecycle(project_id: str) -> dict[str, Any]:
    """Return the full sol ↔ embedded lifecycle status for a project."""
    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")

    threshold = cfg.get("retrain_threshold", {})
    min_new_patterns: int = threshold.get("new_patterns", 200)

    # Feedback since last training
    new_feedback_row = fetchone(
        """SELECT COUNT(*) as n FROM rag_feedback
           WHERE project_id = ?
           AND created_at > COALESCE(
               (SELECT finished_at FROM runs
                WHERE project_id = ? AND status = 'completed'
                ORDER BY finished_at DESC LIMIT 1),
               '1970-01-01T00:00:00Z'
           )""",
        (project_id, project_id),
    )
    new_since_last: int = (new_feedback_row or {}).get("n", 0)

    last_run = _last_training_run(project_id)
    last_export = _last_embedded_export(project_id)
    rag_total = _rag_pattern_count(project_id)

    # Embedded versions history
    export_dir = Path("exports/embedded")
    versions: list[dict[str, Any]] = []
    if export_dir.exists():
        import json  # noqa: PLC0415
        for bundle_dir in sorted(export_dir.iterdir(), reverse=True):
            if not bundle_dir.is_dir() or not bundle_dir.name.startswith(project_id):
                continue
            m_path = bundle_dir / "manifest.json"
            if m_path.exists():
                with m_path.open() as fh:
                    m = json.load(fh)
                versions.append({
                    "bundle_id":   m.get("bundle_id"),
                    "created_at":  m.get("created_at"),
                    "corpus_hash": m.get("rag_corpus", {}).get("hash"),
                    "val_loss":    m.get("training", {}).get("val_loss"),
                })
            if len(versions) >= 10:
                break

    return {
        "project_id":     project_id,
        "rag": {
            "total_patterns":       rag_total,
            "new_since_last_train": new_since_last,
            "retrain_threshold":    min_new_patterns,
            "progress_pct":         min(100, round(new_since_last / max(min_new_patterns, 1) * 100)),
        },
        "last_training": {
            "run_id":      last_run["id"] if last_run else None,
            "finished_at": last_run["finished_at"] if last_run else None,
            "val_loss":    last_run["val_loss"] if last_run else None,
        },
        "pending_retrain":    _pending_retrain(project_id),
        "last_embedded":      last_export,
        "embedded_versions":  versions,
    }


class ExportRequest(BaseModel):
    model_id: str
    include_adapter: bool = True


@router.post("/{project_id}/lifecycle/export")
def trigger_export(project_id: str, body: ExportRequest) -> dict[str, Any]:
    """Create a new embedded bundle from a production model."""
    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")
    try:
        return export_bundle(body.model_id, include_adapter=body.include_adapter)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{project_id}/lifecycle/ingest-feedback")
def trigger_feedback_ingest(project_id: str) -> dict[str, Any]:
    """Scan raw/feedback/ and ingest all pending entries into RAG."""
    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return ingest_feedback_dir(project_id)
