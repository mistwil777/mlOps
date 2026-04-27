"""Model delivery — package info and file downloads."""

from __future__ import annotations

import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.db import fetchall, fetchone

router = APIRouter(prefix="/api/models", tags=["delivery"])


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    return round(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6, 2)


@router.get("/{model_id}/package")
def get_package_info(model_id: str):
    row = fetchone(
        "SELECT m.*, r.model_name, r.adapter_path, r.val_loss, r.iters, r.lora_rank, r.learning_rate, p.name as project_name FROM models m LEFT JOIN runs r ON m.run_id = r.id LEFT JOIN projects p ON m.project_id = p.id WHERE m.id = ?",
        (model_id,),
    )
    if not row:
        raise HTTPException(404, f"Model '{model_id}' not found")

    fused_path   = Path(row.get("fused_path") or "")
    adapter_path = Path(row.get("adapter_path") or "")

    evals = fetchall(
        "SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at DESC",
        (row["run_id"],),
    )
    changelog = fetchall(
        "SELECT id, started_at, status, val_loss, train_loss, iters FROM runs WHERE project_id = ? ORDER BY started_at DESC LIMIT 10",
        (row["project_id"],),
    )

    return {
        "model_id":     model_id,
        "project_name": row.get("project_name"),
        "model_name":   row.get("model_name"),
        "deployed_at":  row.get("deployed_at"),
        "fused": {
            "path":      str(fused_path),
            "exists":    fused_path.exists(),
            "size_mb":   _dir_size_mb(fused_path),
        },
        "adapter": {
            "path":      str(adapter_path),
            "exists":    adapter_path.exists(),
            "size_mb":   _dir_size_mb(adapter_path),
        },
        "evaluations": evals,
        "changelog":   changelog,
        "run": {
            "val_loss":      row.get("val_loss"),
            "iters":         row.get("iters"),
            "lora_rank":     row.get("lora_rank"),
            "learning_rate": row.get("learning_rate"),
        },
    }


@router.get("/{model_id}/download/fused")
def download_fused(model_id: str):
    row = fetchone("SELECT fused_path, project_id FROM models WHERE id = ?", (model_id,))
    if not row:
        raise HTTPException(404)
    p = Path(row["fused_path"])
    if not p.exists():
        raise HTTPException(404, "Fused model not found on disk")

    tmp = NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in p.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(p.parent))

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{row['project_id']}-fused-{model_id}.zip",
    )


@router.get("/{model_id}/download/adapter")
def download_adapter(model_id: str):
    row = fetchone(
        "SELECT r.adapter_path, m.project_id FROM models m LEFT JOIN runs r ON m.run_id = r.id WHERE m.id = ?",
        (model_id,),
    )
    if not row:
        raise HTTPException(404)
    p = Path(row.get("adapter_path") or "")
    if not p.exists():
        raise HTTPException(404, "Adapter not found on disk")

    tmp = NamedTemporaryFile(suffix=".zip", delete=False)
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in p.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(p.parent))

    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"{row['project_id']}-adapter-{model_id}.zip",
    )
