"""Training runs — launch, cancel, list, detail, metrics."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.config import get_project_config
from api.db import execute, fetchall, fetchone

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Active subprocesses keyed by run_id
_PROCS: dict[str, subprocess.Popen] = {}

MLFLOW_URI = "http://localhost:5001"


class RunCreate(BaseModel):
    project_id: str
    experiment_name: str = ""
    iters: int = 1000
    batch_size: int = 8
    learning_rate: float = 1e-4
    lora_rank: int = 16
    lora_layers: int = 16
    use_rag: bool = True
    resume: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _mlflow_metrics(mlflow_run_id: str) -> list[dict[str, Any]]:
    """Fetch metric history from MLflow REST API."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(
                f"{MLFLOW_URI}/api/2.0/mlflow/metrics/get-history",
                params={"run_id": mlflow_run_id, "metric_key": "train_loss"},
            )
            if r.status_code != 200:
                return []
            data = r.json()
            train = [{"step": m["step"], "train_loss": m["value"]} for m in data.get("metrics", [])]

            r2 = await client.get(
                f"{MLFLOW_URI}/api/2.0/mlflow/metrics/get-history",
                params={"run_id": mlflow_run_id, "metric_key": "val_loss"},
            )
            val_map = {}
            if r2.status_code == 200:
                for m in r2.json().get("metrics", []):
                    val_map[m["step"]] = m["value"]

            for entry in train:
                entry["val_loss"] = val_map.get(entry["step"])
            return train
    except Exception:
        return []


@router.get("")
def list_runs(
    project_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
):
    sql = "SELECT * FROM runs"
    params: list[Any] = []
    conditions = []
    if project_id:
        conditions.append("project_id = ?")
        params.append(project_id)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(limit)
    return fetchall(sql, tuple(params))


@router.get("/{run_id}")
def get_run(run_id: str):
    row = fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(404, f"Run '{run_id}' not found")
    return row


@router.get("/{run_id}/metrics")
async def get_run_metrics(run_id: str):
    row = fetchone("SELECT mlflow_run_id, train_loss, val_loss FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(404, f"Run '{run_id}' not found")
    mlflow_run_id = row.get("mlflow_run_id")
    if mlflow_run_id:
        history = await _mlflow_metrics(mlflow_run_id)
        if history:
            return history
    # Fallback: single point from SQLite
    result = []
    if row.get("train_loss") is not None:
        result.append({"step": 0, "train_loss": row["train_loss"], "val_loss": row.get("val_loss")})
    return result


@router.get("/evaluations")
def list_evaluations(
    project_id: str | None = Query(None),
    limit: int = Query(50, le=200),
):
    """List evaluations across all runs, optionally filtered by project."""
    sql = """
        SELECT e.*, r.project_id, r.experiment_name, r.model_name,
               r.train_loss, r.val_loss AS run_val_loss
        FROM evaluations e
        JOIN runs r ON r.id = e.run_id
    """
    params: list[Any] = []
    if project_id:
        sql += " WHERE r.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY e.created_at DESC LIMIT ?"
    params.append(limit)
    return fetchall(sql, tuple(params))


@router.get("/{run_id}/evaluations")
def get_run_evaluations(run_id: str):
    return fetchall("SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at DESC", (run_id,))


@router.post("", status_code=201)
def create_run(body: RunCreate):
    cfg = get_project_config(body.project_id)
    if not cfg:
        raise HTTPException(404, f"Project '{body.project_id}' not found")

    run_id = str(uuid.uuid4())[:8]
    experiment_name = body.experiment_name or f"{body.project_id}-lora"
    model_name = cfg.get("model", "qwen2.5-coder-7b-4bit")
    adapter_path = f"adapters/{body.project_id}/{run_id}"
    data_path = cfg.get("data", {}).get("fine_tuning_ready", f"datalake/projects/{body.project_id}/fine-tuning-ready/")

    cmd = [
        sys.executable, "-m", "pipeline.train",
        "--project", body.project_id,
        "--experiment", experiment_name,
        "--iters", str(body.iters),
    ]
    if body.resume:
        cmd.append("--resume")

    now = _now()
    execute(
        """INSERT INTO runs (id, project_id, experiment_name, model_name, status,
               iters, batch_size, learning_rate, lora_rank, adapter_path, started_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, body.project_id, experiment_name, model_name, "running",
         body.iters, body.batch_size, body.learning_rate, body.lora_rank, adapter_path, now),
    )

    log_path = Path(f"logs/{run_id}.log")
    log_path.parent.mkdir(exist_ok=True)

    proc = subprocess.Popen(
        cmd,
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        cwd=Path(".").resolve(),
    )
    _PROCS[run_id] = proc

    # Background watcher to update status on exit
    asyncio.create_task(_watch_proc(run_id, proc))  # noqa: RUF006

    return {"run_id": run_id, "status": "running", "log": str(log_path)}


async def _watch_proc(run_id: str, proc: subprocess.Popen) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, proc.wait)
    status = "completed" if proc.returncode == 0 else "failed"
    now = _now()
    execute(
        "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
        (status, now, run_id),
    )
    _PROCS.pop(run_id, None)


@router.post("/{run_id}/cancel")
def cancel_run(run_id: str):
    """Cancel a running run without deleting it."""
    proc = _PROCS.get(run_id)
    if proc:
        proc.terminate()
        _PROCS.pop(run_id, None)
    execute(
        "UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'running'",
        (_now(), run_id),
    )
    return {"run_id": run_id, "status": "cancelled"}


@router.delete("/{run_id}", status_code=204)
def delete_run(run_id: str):
    """Archive (soft-delete) a run — moves it to archived_runs, keeps it in history."""
    row = fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    if not row:
        raise HTTPException(404, f"Run '{run_id}' not found")

    # Cancel if still running
    proc = _PROCS.get(run_id)
    if proc:
        proc.terminate()
        _PROCS.pop(run_id, None)

    execute(
        """INSERT OR IGNORE INTO archived_runs
           SELECT *, ? as archived_at FROM runs WHERE id = ?""",
        (_now(), run_id),
    )
    execute("DELETE FROM runs WHERE id = ?", (run_id,))


@router.post("/{run_id}/evaluate", status_code=202)
def launch_evaluation(run_id: str):
    row = fetchone("SELECT project_id FROM runs WHERE id = ? AND status = 'completed'", (run_id,))
    if not row:
        raise HTTPException(404, "Run not found or not completed")

    eval_id = str(uuid.uuid4())[:8]
    cmd = [sys.executable, "-m", "pipeline.evaluate", "--project", row["project_id"], "--run-id", run_id]
    log_path = Path(f"logs/eval-{eval_id}.log")

    subprocess.Popen(cmd, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, cwd=Path(".").resolve())
    return {"eval_id": eval_id, "status": "started"}
