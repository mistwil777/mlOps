"""Dataset stats per project per stage."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.config import get_project_config
from api.db import fetchall

router = APIRouter(prefix="/api/projects", tags=["datasets"])


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with path.open(errors="replace") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def _dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return round(total / 1e6, 2)


@router.get("/{project_id}/datasets")
def get_datasets(project_id: str):
    cfg = get_project_config(project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{project_id}' not found")

    data_cfg = cfg.get("data", {})
    stages = [
        ("raw",               data_cfg.get("raw",               f"datalake/projects/{project_id}/raw/")),
        ("processed",         data_cfg.get("processed",         f"datalake/projects/{project_id}/processed/")),
        ("fine-tuning-ready", data_cfg.get("fine_tuning_ready", f"datalake/projects/{project_id}/fine-tuning-ready/")),
    ]

    result = []
    for stage_name, stage_path in stages:
        p = Path(stage_path)
        jsonl_files = list(p.glob("*.jsonl")) if p.exists() else []

        # Prefer DB record if available
        db_rows = fetchall(
            "SELECT * FROM datasets WHERE project_id = ? AND stage = ? ORDER BY created_at DESC LIMIT 1",
            (project_id, stage_name),
        )
        db_row = db_rows[0] if db_rows else {}

        n_pairs = db_row.get("n_pairs") or sum(_count_jsonl(f) for f in jsonl_files)
        result.append({
            "stage":       stage_name,
            "path":        stage_path,
            "n_pairs":     n_pairs,
            "n_train":     db_row.get("n_train"),
            "n_valid":     db_row.get("n_valid"),
            "avg_quality": db_row.get("avg_quality"),
            "size_mb":     _dir_size_mb(p),
            "files":       [str(f.name) for f in jsonl_files],
        })

    return result
