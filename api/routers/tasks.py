"""Task status router — GET /tasks/{id} and GET /tasks/{id}/stream (SSE)."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from api.celery_app import celery_app

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _task_info(task_id: str) -> dict[str, Any]:
    """Return normalized task status from Celery result backend."""
    result = celery_app.AsyncResult(task_id)
    state = result.state  # PENDING | STARTED | PROGRESS | SUCCESS | FAILURE | REVOKED

    info: dict[str, Any] = {
        "task_id": task_id,
        "state": state,
        "ready": result.ready(),
        "successful": result.successful() if result.ready() else None,
    }

    if state == "PROGRESS":
        meta = result.info or {}
        info.update({
            "current": meta.get("current", 0),
            "total": meta.get("total", 1),
            "message": meta.get("message", ""),
            "lines": meta.get("lines", []),
        })
    elif state == "SUCCESS":
        info["result"] = result.result
    elif state == "FAILURE":
        info["error"] = str(result.result)

    return info


@router.get("/{task_id}")
def get_task_status(task_id: str) -> dict[str, Any]:
    """Poll task status."""
    return _task_info(task_id)


@router.get("/{task_id}/stream")
async def stream_task(task_id: str) -> EventSourceResponse:
    """SSE stream — sends state updates until task is done."""
    result = celery_app.AsyncResult(task_id)
    if result.state == "PENDING":
        # Give Celery 2s to pick it up before 404-ing
        await asyncio.sleep(2)
        if celery_app.AsyncResult(task_id).state == "PENDING":
            raise HTTPException(404, f"Task {task_id} not found or never started")

    async def generator():
        sent_lines = 0
        while True:
            info = _task_info(task_id)
            state = info["state"]

            # Stream new lines for subprocess tasks
            lines = info.get("lines", [])
            for line in lines[sent_lines:]:
                yield {"event": "line", "data": line}
            sent_lines = len(lines)

            yield {"event": "status", "data": __import__("json").dumps(info)}

            if state in ("SUCCESS", "FAILURE", "REVOKED"):
                yield {"event": "done", "data": state}
                break

            await asyncio.sleep(1)

    return EventSourceResponse(generator())


@router.delete("/{task_id}")
def revoke_task(task_id: str) -> dict[str, str]:
    """Cancel a running task (best-effort — cannot kill a running subprocess mid-line)."""
    celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    return {"task_id": task_id, "status": "revoked"}
