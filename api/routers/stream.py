"""SSE streams — live metrics and live logs for a run."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from api.db import fetchone

router = APIRouter(prefix="/api/runs", tags=["stream"])

MLFLOW_URI  = "http://localhost:5001"
POLL_INTERVAL = 2.0   # seconds between MLflow polls


async def _metrics_generator(run_id: str) -> AsyncGenerator[dict, None]:
    """Yield SSE events with train_loss/val_loss until run finishes."""
    seen_steps: set[int] = set()

    while True:
        row = fetchone("SELECT status, mlflow_run_id, train_loss, val_loss FROM runs WHERE id = ?", (run_id,))
        if not row:
            yield {"event": "error", "data": json.dumps({"message": "run not found"})}
            return

        # Fetch metric history from MLflow
        mlflow_run_id = row.get("mlflow_run_id")
        if mlflow_run_id:
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    r_train = await client.get(
                        f"{MLFLOW_URI}/api/2.0/mlflow/metrics/get-history",
                        params={"run_id": mlflow_run_id, "metric_key": "train_loss"},
                    )
                    r_val = await client.get(
                        f"{MLFLOW_URI}/api/2.0/mlflow/metrics/get-history",
                        params={"run_id": mlflow_run_id, "metric_key": "val_loss"},
                    )
                    val_map = {m["step"]: m["value"] for m in r_val.json().get("metrics", [])} if r_val.status_code == 200 else {}
                    if r_train.status_code == 200:
                        for m in r_train.json().get("metrics", []):
                            step = m["step"]
                            if step not in seen_steps:
                                seen_steps.add(step)
                                yield {
                                    "event": "metric",
                                    "data": json.dumps({
                                        "step":       step,
                                        "train_loss": m["value"],
                                        "val_loss":   val_map.get(step),
                                        "status":     row["status"],
                                    }),
                                }
            except Exception:
                pass

        # Always emit status heartbeat
        yield {
            "event": "status",
            "data": json.dumps({"status": row["status"]}),
        }

        if row["status"] not in ("running",):
            return

        await asyncio.sleep(POLL_INTERVAL)


async def _log_generator(run_id: str) -> AsyncGenerator[dict, None]:
    """Tail the run log file and stream new lines as SSE events."""
    log_path = Path(f"logs/{run_id}.log")

    # Wait for log file to appear (max 10s)
    for _ in range(20):
        if log_path.exists():
            break
        await asyncio.sleep(0.5)

    if not log_path.exists():
        yield {"event": "error", "data": json.dumps({"message": "log not found"})}
        return

    with log_path.open(errors="replace") as fh:
        # First emit last 50 existing lines
        existing = fh.readlines()
        for line in existing[-50:]:
            yield {"event": "log", "data": json.dumps({"line": line.rstrip()})}

        while True:
            row = fetchone("SELECT status FROM runs WHERE id = ?", (run_id,))
            status = (row or {}).get("status", "failed")

            line = fh.readline()
            if line:
                yield {"event": "log", "data": json.dumps({"line": line.rstrip()})}
            else:
                if status not in ("running",):
                    return
                await asyncio.sleep(0.3)


@router.get("/{run_id}/stream")
async def stream_metrics(run_id: str):
    return EventSourceResponse(_metrics_generator(run_id))


@router.get("/{run_id}/logs/stream")
async def stream_logs(run_id: str):
    return EventSourceResponse(_log_generator(run_id))
