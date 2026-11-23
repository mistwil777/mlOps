"""RAG index management per project — backed by Qdrant."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from qdrant_client import QdrantClient
from sse_starlette.sse import EventSourceResponse

from pipeline.utils.run_ledger import TERMINAL_STATUSES, get_ledger as _ledger

router = APIRouter(prefix="/api/rag", tags=["rag"])

_QDRANT_HOST = "localhost"
_QDRANT_PORT = 6333


def _qdrant() -> QdrantClient:
    return QdrantClient(host=_QDRANT_HOST, port=_QDRANT_PORT)


def _rag_info(project_id: str) -> dict[str, Any]:
    collection = f"patterns_{project_id}"
    try:
        client = _qdrant()
        existing = [c.name for c in client.get_collections().collections]
        if collection not in existing:
            return {"exists": False, "count": 0}
        info = client.get_collection(collection)
        return {
            "exists": True,
            "count": info.points_count or 0,
            "collection": collection,
        }
    except Exception:
        return {"exists": False, "count": 0}


@router.get("")
async def list_rag_indexes() -> list[dict[str, Any]]:
    """List all existing RAG collections in Qdrant."""
    try:
        client = _qdrant()
        collections = client.get_collections().collections
        result = []
        for c in sorted(collections, key=lambda x: x.name):
            if not c.name.startswith("patterns_"):
                continue
            project_id = c.name.removeprefix("patterns_")
            info = _rag_info(project_id)
            result.append({"project_id": project_id, **info})
        return result
    except Exception as exc:
        raise HTTPException(503, f"Qdrant unavailable: {exc}") from exc


@router.get("/{project_id}")
async def get_rag_index(project_id: str) -> dict[str, Any]:
    return {"project_id": project_id, **_rag_info(project_id)}


@router.post("/{project_id}/build")
async def build_rag_index(project_id: str, rebuild: bool = False) -> dict[str, str]:
    """Build or rebuild a RAG index for a project (async, SSE streaming)."""
    job_id = str(uuid.uuid4())[:8]
    _ledger().create_job(job_id, project_id, "rag-build")

    asyncio.create_task(_run_build(job_id, project_id, rebuild))
    return {"job_id": job_id}


async def _run_build(job_id: str, project_id: str, rebuild: bool) -> None:
    cmd = [
        sys.executable, "-c",
        f"""
import sys
from api.config import get_project_config
from pipeline.rag_index import build_index
cfg = get_project_config('{project_id}')
if cfg is None:
    print(f'ERROR: project config not found: {project_id}', flush=True)
    sys.exit(1)
domain = cfg.get('domain', 'other')
print(f'Building RAG index for project={project_id} domain={{domain}} rebuild={rebuild}...', flush=True)
idx = build_index('{project_id}', domain, rebuild={rebuild})
print(f'Done: {{idx.count}} patterns indexed', flush=True)
""",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            _ledger().log(job_id, line)

        await proc.wait()
        _ledger().set_fields(job_id, returncode=proc.returncode)
        _ledger().set_status(job_id, "completed" if proc.returncode == 0 else "failed")
    except Exception as exc:
        _ledger().log(job_id, f"ERROR: {exc}", level="error")
        _ledger().set_status(job_id, "failed")


@router.get("/build/{job_id}/stream")
async def stream_build(job_id: str):
    if not _ledger().exists(job_id):
        raise HTTPException(404, "Job not found")

    async def generator():
        last_seq = 0
        while True:
            for seq, line in _ledger().events_since(job_id, last_seq):
                yield {"event": "log", "data": json.dumps({"line": line})}
                last_seq = seq
            status = _ledger().get_job(job_id)["status"]
            if status in TERMINAL_STATUSES:
                for seq, line in _ledger().events_since(job_id, last_seq):  # final drain
                    yield {"event": "log", "data": json.dumps({"line": line})}
                    last_seq = seq
                yield {"event": "done", "data": json.dumps({"status": status})}
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())


@router.delete("/{project_id}")
async def drop_rag_index(project_id: str) -> dict[str, str]:
    """Drop a project's RAG collection from Qdrant."""
    collection = f"patterns_{project_id}"
    try:
        client = _qdrant()
        existing = [c.name for c in client.get_collections().collections]
        if collection not in existing:
            raise HTTPException(404, f"RAG index for project '{project_id}' not found")
        client.delete_collection(collection)
        return {"ok": f"RAG index for '{project_id}' deleted"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


class SearchRequest(BaseModel):
    query: str
    k: int = 5
    domain_filter: str | None = None


@router.post("/{project_id}/search")
async def search_rag(project_id: str, req: SearchRequest) -> dict[str, Any]:
    """Perform a semantic search against a project's RAG index."""
    info = _rag_info(project_id)
    if not info.get("exists"):
        raise HTTPException(404, f"No RAG index for project '{project_id}'")

    try:
        from pipeline.rag_index import RagIndex  # noqa: PLC0415
        idx = RagIndex(project_id)
        results = idx.search(req.query, k=req.k, domain_filter=req.domain_filter)
        return {"query": req.query, "results": results, "total": len(results)}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
