"""Deployed model catalog — fuse, serve, stop, compare."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.db import execute, fetchall, fetchone

router = APIRouter(prefix="/api/models", tags=["models"])

_SERVING_PROCS: dict[str, subprocess.Popen] = {}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DeployRequest(BaseModel):
    project_id: str
    run_id: str
    port: int = 8080


@router.get("")
def list_models(project_id: str | None = None):
    sql = "SELECT m.*, r.model_name, r.val_loss, r.train_loss, p.name as project_name FROM models m LEFT JOIN runs r ON m.run_id = r.id LEFT JOIN projects p ON m.project_id = p.id"
    params: tuple = ()
    if project_id:
        sql += " WHERE m.project_id = ?"
        params = (project_id,)
    sql += " ORDER BY m.deployed_at DESC"
    rows = fetchall(sql, params)
    # Enrich with disk size
    for row in rows:
        p = Path(row.get("fused_path") or "")
        row["size_mb"] = round(sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6, 1) if p.exists() else None
    return rows


@router.get("/{model_id}")
def get_model(model_id: str):
    row = fetchone(
        "SELECT m.*, r.model_name, r.val_loss, r.iters, p.name as project_name FROM models m LEFT JOIN runs r ON m.run_id = r.id LEFT JOIN projects p ON m.project_id = p.id WHERE m.id = ?",
        (model_id,),
    )
    if not row:
        raise HTTPException(404, f"Model '{model_id}' not found")
    return row


@router.post("", status_code=201)
def deploy_model(body: DeployRequest):
    run = fetchone("SELECT * FROM runs WHERE id = ? AND status = 'completed'", (body.run_id,))
    if not run:
        raise HTTPException(404, "Run not found or not completed")

    model_id = str(uuid.uuid4())[:8]
    fused_path = f"models/{body.project_id}/{model_id}"

    cmd = [
        sys.executable, "-m", "pipeline.deploy",
        "--project", body.project_id,
        "--run-id", body.run_id,
        "--output", fused_path,
        "--port", str(body.port),
    ]
    log_path = Path(f"logs/deploy-{model_id}.log")
    subprocess.Popen(cmd, stdout=open(log_path, "w"), stderr=subprocess.STDOUT, cwd=Path(".").resolve())

    now = _now()
    execute(
        "INSERT INTO models (id, project_id, run_id, fused_path, serving_port, status, deployed_at) VALUES (?,?,?,?,?,?,?)",
        (model_id, body.project_id, body.run_id, fused_path, body.port, "stopped", now),
    )
    return {"model_id": model_id, "status": "deploying", "log": str(log_path)}


@router.post("/{model_id}/serve")
def serve_model(model_id: str):
    row = fetchone("SELECT * FROM models WHERE id = ?", (model_id,))
    if not row:
        raise HTTPException(404, f"Model '{model_id}' not found")

    fused_path = row["fused_path"]
    port = row["serving_port"] or 8080

    if not Path(fused_path).exists():
        raise HTTPException(409, f"Fused model path not found: {fused_path}")

    cmd = ["mlx_lm.server", "--model", fused_path, "--port", str(port)]
    proc = subprocess.Popen(cmd, cwd=Path(".").resolve())
    _SERVING_PROCS[model_id] = proc

    execute("UPDATE models SET status = 'serving' WHERE id = ?", (model_id,))
    return {"model_id": model_id, "status": "serving", "port": port}


@router.post("/{model_id}/stop")
def stop_model(model_id: str):
    proc = _SERVING_PROCS.pop(model_id, None)
    if proc:
        proc.terminate()
    execute(
        "UPDATE models SET status = 'stopped', stopped_at = ? WHERE id = ?",
        (_now(), model_id),
    )
    return {"model_id": model_id, "status": "stopped"}


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str):
    stop_model(model_id)
    execute("DELETE FROM models WHERE id = ?", (model_id,))
