"""RAG index management per project."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/rag", tags=["rag"])

_JOBS: dict[str, dict[str, Any]] = {}


def _rag_info(project_id: str) -> dict[str, Any]:
    db_path = Path(f"lancedb/{project_id}")
    if not db_path.exists():
        return {"exists": False, "count": 0, "size_mb": 0, "tables": []}

    tables = [p.name for p in db_path.iterdir() if p.is_dir()]
    total_size = sum(f.stat().st_size for f in db_path.rglob("*") if f.is_file())

    count = 0
    try:
        import lancedb  # noqa: PLC0415
        db = lancedb.connect(str(db_path))
        tbl = db.open_table("patterns")
        count = tbl.count_rows()
    except Exception:
        pass

    return {
        "exists": True,
        "count": count,
        "size_mb": round(total_size / 1e6, 2),
        "tables": tables,
        "db_path": str(db_path),
    }


@router.get("")
async def list_rag_indexes() -> list[dict[str, Any]]:
    """List all existing RAG indexes."""
    base = Path("lancedb")
    if not base.exists():
        return []

    result = []
    for d in sorted(base.iterdir()):
        if d.is_dir():
            info = _rag_info(d.name)
            result.append({"project_id": d.name, **info})
    return result


@router.get("/{project_id}")
async def get_rag_index(project_id: str) -> dict[str, Any]:
    return {"project_id": project_id, **_rag_info(project_id)}


@router.post("/{project_id}/build")
async def build_rag_index(project_id: str, rebuild: bool = False) -> dict[str, str]:
    """Build or rebuild a RAG index for a project (async, SSE streaming)."""
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": project_id}

    asyncio.create_task(_run_build(job_id, project_id, rebuild))
    return {"job_id": job_id}


async def _run_build(job_id: str, project_id: str, rebuild: bool) -> None:
    rebuild_flag = ["--rebuild"] if rebuild else []
    cmd = [
        sys.executable, "-c",
        f"""
import yaml, sys
from pathlib import Path
from pipeline.rag_index import build_index
p = Path('config/projects/{project_id}.yaml')
if not p.exists():
    print(f'ERROR: project config not found: {{p}}', flush=True)
    sys.exit(1)
cfg = yaml.safe_load(p.open())
domain = cfg.get('domain', 'code')
print(f'Building RAG index for project={project_id} domain={{domain}} rebuild={rebuild}…', flush=True)
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
            _JOBS[job_id]["lines"].append(line)

        await proc.wait()
        _JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")


@router.get("/build/{job_id}/stream")
async def stream_build(job_id: str):
    if job_id not in _JOBS:
        raise HTTPException(404, "Job not found")

    async def generator():
        sent = 0
        while True:
            job = _JOBS[job_id]
            while sent < len(job["lines"]):
                yield {"event": "log", "data": json.dumps({"line": job["lines"][sent]})}
                sent += 1
            if job["status"] != "running":
                yield {"event": "done", "data": json.dumps({"status": job["status"]})}
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())


@router.delete("/{project_id}")
async def drop_rag_index(project_id: str) -> dict[str, str]:
    """Drop a RAG index (removes LanceDB directory)."""
    db_path = Path(f"lancedb/{project_id}")
    if not db_path.exists():
        raise HTTPException(404, f"RAG index for project '{project_id}' not found")

    import shutil  # noqa: PLC0415
    shutil.rmtree(db_path)
    return {"ok": f"RAG index for '{project_id}' deleted"}


class SearchRequest(BaseModel):
    query: str
    k: int = 5
    domain_filter: str | None = None


@router.post("/{project_id}/search")
async def search_rag(project_id: str, req: SearchRequest) -> dict[str, Any]:
    """Perform a semantic search against a project's RAG index."""
    db_path = Path(f"lancedb/{project_id}")
    if not db_path.exists():
        raise HTTPException(404, f"No RAG index for project '{project_id}'")

    try:
        from pipeline.rag_index import RagIndex  # noqa: PLC0415
        idx = RagIndex(project_id)
        results = idx.search(req.query, k=req.k, domain_filter=req.domain_filter)
        return {"query": req.query, "results": results, "total": len(results)}
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
