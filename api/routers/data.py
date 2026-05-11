"""Data management: AWA collect, human review, cleaning pipeline, manual ingestion, lineage."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from api.db import execute, fetchall

router = APIRouter(prefix="/api/data", tags=["data"])

_JOBS: dict[str, dict[str, Any]] = {}  # in-memory job tracker
_JOB_TTL_S = 3600  # purge completed jobs after 1 hour


def _purge_old_jobs() -> None:
    now = datetime.now(timezone.utc).timestamp()
    to_del = [jid for jid, j in _JOBS.items()
              if j["status"] != "running" and now - j.get("finished_at", now) > _JOB_TTL_S]
    for jid in to_del:
        del _JOBS[jid]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_project_config(project_id: str) -> dict[str, Any]:
    p = Path("config/projects") / f"{project_id}.yaml"
    if not p.exists():
        raise HTTPException(404, f"Project config not found: {project_id}")
    with p.open() as fh:
        return yaml.safe_load(fh)


def _list_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


import re as _re
_PROJECT_ID_RE = _re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


def _validate_project_id(project_id: str) -> None:
    if not _PROJECT_ID_RE.match(project_id):
        raise HTTPException(400, f"Invalid project_id '{project_id}' — only [a-z0-9_-] allowed")


def _pair_path(project_id: str, stage: str) -> Path:
    _validate_project_id(project_id)
    return Path(f"datalake/projects/{project_id}/{stage}/pairs_raw.jsonl")


# ── AWA Collect ───────────────────────────────────────────────────────────────

class CollectRequest(BaseModel):
    project_id: str
    topic: str
    max_pairs: int = 50
    domain: Optional[str] = None
    task_type: Optional[str] = None


@router.post("/collect")
async def start_collect(req: CollectRequest) -> dict[str, str]:
    try:
        config = _load_project_config(req.project_id)
    except HTTPException:
        config = {}
    if req.domain:
        config.setdefault("domain", req.domain)
    if req.task_type:
        config.setdefault("task_type", req.task_type)
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id}

    asyncio.create_task(_run_collect(job_id, req.project_id, req.topic, req.max_pairs, config))
    return {"job_id": job_id}


async def _run_collect(
    job_id: str,
    project_id: str,
    topic: str,
    max_pairs: int,
    config: dict[str, Any],
) -> None:
    domain    = config.get("domain", "code")
    task_type = config.get("task_type", "generation")
    output    = Path(f"datalake/projects/{project_id}/raw/pairs_raw.jsonl")
    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "agents.web_collector",
        "--project",   project_id,
        "--topic",     topic,
        "--domain",    domain,
        "--task-type", task_type,
        "--max-pages", str(max_pairs),
        "--output",    str(output),
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
        _JOBS[job_id]["returncode"] = proc.returncode
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        _purge_old_jobs()


@router.get("/collect/{job_id}/stream")
async def stream_collect(job_id: str):
    if job_id not in _JOBS:
        raise HTTPException(404, "Job not found")

    async def generator():
        sent = 0
        while True:
            job = _JOBS[job_id]
            lines = job["lines"]
            while sent < len(lines):
                yield {"event": "log", "data": json.dumps({"line": lines[sent]})}
                sent += 1
            if job["status"] != "running":
                yield {"event": "done", "data": json.dumps({"status": job["status"]})}
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())


@router.get("/collect/{job_id}/status")
async def collect_status(job_id: str) -> dict[str, Any]:
    if job_id not in _JOBS:
        raise HTTPException(404, "Job not found")
    j = _JOBS[job_id]
    return {"status": j["status"], "lines": len(j["lines"])}


# ── Human Review ──────────────────────────────────────────────────────────────

class ReviewAction(BaseModel):
    index: int
    action: str        # "validate" | "reject" | "correct"
    corrected_prompt: str | None = None
    corrected_completion: str | None = None


@router.get("/review/{project_id}")
async def list_pairs_for_review(
    project_id: str,
    stage: str = "raw",
    page: int = 0,
    page_size: int = 20,
    status: str | None = None,
) -> dict[str, Any]:
    _load_project_config(project_id)
    raw_path = _pair_path(project_id, stage)
    records  = _list_jsonl(raw_path)

    if status == "pending":
        records = [r for r in records if r.get("review_status") not in ("validate", "reject", "correct")]
    elif status in ("validate", "reject", "correct"):
        records = [r for r in records if r.get("review_status") == status]

    total  = len(records)
    sliced = records[page * page_size : (page + 1) * page_size]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": sliced,
    }


@router.post("/review/{project_id}")
async def apply_review(project_id: str, action: ReviewAction) -> dict[str, Any]:
    _load_project_config(project_id)
    raw_path = _pair_path(project_id, "raw")
    records  = _list_jsonl(raw_path)

    if action.index < 0 or action.index >= len(records):
        raise HTTPException(400, f"Index {action.index} out of range (total={len(records)})")

    r = dict(records[action.index])
    r["review_status"] = action.action

    if action.action == "correct":
        if action.corrected_prompt is not None:
            r["prompt"] = action.corrected_prompt
        if action.corrected_completion is not None:
            r["completion"] = action.corrected_completion

    records[action.index] = r
    _write_jsonl(raw_path, records)

    return {"ok": True, "index": action.index, "status": action.action}


@router.get("/review/{project_id}/stats")
async def review_stats(project_id: str) -> dict[str, Any]:
    _load_project_config(project_id)
    raw_path = _pair_path(project_id, "raw")
    records  = _list_jsonl(raw_path)

    total     = len(records)
    validated = sum(1 for r in records if r.get("review_status") == "validate")
    rejected  = sum(1 for r in records if r.get("review_status") == "reject")
    corrected = sum(1 for r in records if r.get("review_status") == "correct")
    pending   = total - validated - rejected - corrected

    return {
        "total": total,
        "validated": validated,
        "rejected": rejected,
        "corrected": corrected,
        "pending": pending,
    }


# ── Cleaning Pipeline ─────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    project_id: str
    use_llm_score: bool = False
    threshold: float = 0.7


@router.post("/pipeline")
async def run_pipeline(req: PipelineRequest) -> dict[str, str]:
    _load_project_config(req.project_id)
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id}

    asyncio.create_task(_run_pipeline(job_id, req))
    return {"job_id": job_id}


async def _run_pipeline(job_id: str, req: PipelineRequest) -> None:
    cmd = [
        sys.executable, "-m", "pipeline.clean",
        "--project", req.project_id,
        "--score", str(req.use_llm_score).lower(),
        "--threshold", str(req.threshold),
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
        _JOBS[job_id]["returncode"] = proc.returncode
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        _purge_old_jobs()


@router.get("/pipeline/{job_id}/stream")
async def stream_pipeline(job_id: str):
    if job_id not in _JOBS:
        raise HTTPException(404, "Job not found")

    async def generator():
        sent = 0
        while True:
            job = _JOBS[job_id]
            lines = job["lines"]
            while sent < len(lines):
                yield {"event": "log", "data": json.dumps({"line": lines[sent]})}
                sent += 1
            if job["status"] != "running":
                yield {"event": "done", "data": json.dumps({"status": job["status"]})}
                return
            await asyncio.sleep(0.5)

    return EventSourceResponse(generator())


# ── Data Health ───────────────────────────────────────────────────────────────

@router.get("/health/{project_id}")
async def data_health(project_id: str) -> dict[str, Any]:
    _load_project_config(project_id)
    base = Path(f"datalake/projects/{project_id}")

    def _stage_info(stage: str, filename: str = "pairs_raw.jsonl") -> dict[str, Any]:
        p = base / stage / filename
        if not p.exists():
            return {"exists": False, "count": 0, "size_mb": 0}
        records = _list_jsonl(p)
        size_mb = round(p.stat().st_size / 1e6, 2)
        scores  = [r.get("quality_score") for r in records if r.get("quality_score") is not None]
        reviewed_statuses = [r.get("review_status") for r in records if r.get("review_status")]
        return {
            "exists": True,
            "count": len(records),
            "size_mb": size_mb,
            "avg_quality": round(sum(scores) / len(scores), 3) if scores else None,
            "scored_count": len(scores),
            "reviewed_count": len(reviewed_statuses),
        }

    def _split_info(stage: str) -> dict[str, Any]:
        train_p = base / stage / "train.jsonl"
        valid_p = base / stage / "valid.jsonl"
        return {
            "train": len(_list_jsonl(train_p)) if train_p.exists() else 0,
            "valid": len(_list_jsonl(valid_p)) if valid_p.exists() else 0,
        }

    raw       = _stage_info("raw")
    processed = _stage_info("processed")
    ftr       = _stage_info("fine-tuning-ready")
    splits    = _split_info("fine-tuning-ready")

    funnel = {
        "raw":              raw["count"],
        "processed":        processed["count"],
        "fine_tuning_ready": splits["train"] + splits["valid"],
    }

    alerts: list[str] = []
    if raw["count"] == 0:
        alerts.append("Aucune donnée collectée — lancez AWA pour collecter des paires.")
    elif processed["count"] == 0:
        alerts.append("Données brutes disponibles — lancez le pipeline de nettoyage.")
    elif splits["train"] == 0:
        alerts.append("Nettoyage terminé mais aucun split train/valid — vérifiez le seuil qualité.")

    if raw["count"] > 0 and processed.get("avg_quality") is not None and processed["avg_quality"] < 0.6:
        alerts.append(f"Qualité moyenne basse ({processed['avg_quality']:.2f}) — vérifiez les données.")

    # Lineage summary
    lineage = fetchall(
        "SELECT source_type, COUNT(*) as n, AVG(quality_score) as avg_q "
        "FROM data_lineage WHERE project_id = ? GROUP BY source_type",
        (project_id,),
    )

    return {
        "project_id": project_id,
        "funnel": funnel,
        "stages": {
            "raw": raw,
            "processed": processed,
            "fine_tuning_ready": {**ftr, **splits},
        },
        "alerts": alerts,
        "lineage_summary": lineage,
    }


# ── Manual Ingestion ──────────────────────────────────────────────────────────

@router.post("/ingest/{project_id}")
async def ingest_file(
    project_id: str,
    file: UploadFile = File(...),
    source_description: str = "manual_upload",
) -> dict[str, Any]:
    """Accept a JSONL file or ZIP of JSONL files and append to raw stage."""
    _load_project_config(project_id)

    raw_dir = Path(f"datalake/projects/{project_id}/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / "pairs_raw.jsonl"

    content = await file.read()
    if len(content) > 100 * 1024 * 1024:  # 100 MB max
        raise HTTPException(413, "File too large (max 100 MB)")
    filename = file.filename or "upload.jsonl"

    records_new: list[dict[str, Any]] = []

    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            total_uncompressed = sum(i.file_size for i in zf.infolist())
            if total_uncompressed > 500 * 1024 * 1024:  # 500 MB décompressé max
                raise HTTPException(413, "Zip content too large when decompressed (max 500 MB)")
            for name in zf.namelist():
                if name.endswith(".jsonl"):
                    data = zf.read(name).decode("utf-8", errors="replace")
                    for line in data.splitlines():
                        line = line.strip()
                        if line:
                            try:
                                records_new.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
    elif filename.endswith(".jsonl"):
        for line in content.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    records_new.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    else:
        raise HTTPException(400, "Only .jsonl or .zip files are accepted")

    if not records_new:
        raise HTTPException(400, "No valid JSONL records found in uploaded file")

    # Validate minimal structure
    for r in records_new:
        if "prompt" not in r or "completion" not in r:
            raise HTTPException(400, "Each record must have 'prompt' and 'completion' fields")

    # Append to raw file
    existing = _list_jsonl(raw_path)
    ts = datetime.now(timezone.utc).isoformat()
    tagged = [{**r, "meta": {**r.get("meta", {}), "source": source_description, "ingested_at": ts}} for r in records_new]
    _write_jsonl(raw_path, existing + tagged)

    # Track lineage
    for r in tagged:
        _track_lineage(project_id, r, source_description)

    return {
        "ingested": len(tagged),
        "total_raw": len(existing) + len(tagged),
        "source": source_description,
    }


# ── Data Lineage ──────────────────────────────────────────────────────────────

def _ensure_lineage_table() -> None:
    try:
        execute(
            """CREATE TABLE IF NOT EXISTS data_lineage (
                id           TEXT PRIMARY KEY,
                project_id   TEXT NOT NULL,
                source_type  TEXT,
                source_url   TEXT,
                collected_at TEXT,
                quality_score REAL,
                review_status TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER
            )""",
        )
    except Exception:
        pass


def _track_lineage(project_id: str, record: dict[str, Any], source_type: str) -> None:
    _ensure_lineage_table()
    try:
        meta = record.get("meta", {})
        prompt_tokens = len(record.get("prompt", "").split())
        completion_tokens = len(record.get("completion", "").split())
        execute(
            "INSERT OR IGNORE INTO data_lineage VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                project_id,
                source_type,
                meta.get("source_url", ""),
                meta.get("ingested_at") or datetime.now(timezone.utc).isoformat(),
                record.get("quality_score"),
                record.get("review_status"),
                prompt_tokens,
                completion_tokens,
            ),
        )
    except Exception:
        pass


@router.get("/lineage/{project_id}")
async def get_lineage(project_id: str, limit: int = 100) -> list[dict[str, Any]]:
    _load_project_config(project_id)
    _ensure_lineage_table()
    return fetchall(
        "SELECT * FROM data_lineage WHERE project_id = ? ORDER BY collected_at DESC LIMIT ?",
        (project_id, limit),
    )
