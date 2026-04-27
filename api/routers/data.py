"""Data management: AWA collect, human review, cleaning pipeline."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/data", tags=["data"])

_JOBS: dict[str, dict[str, Any]] = {}  # in-memory job tracker


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


def _pair_path(project_id: str, stage: str) -> Path:
    return Path(f"datalake/projects/{project_id}/{stage}/pairs_raw.jsonl")


# ── AWA Collect ───────────────────────────────────────────────────────────────

class CollectRequest(BaseModel):
    project_id: str
    topic: str
    max_pairs: int = 50


@router.post("/collect")
async def start_collect(req: CollectRequest) -> dict[str, str]:
    config = _load_project_config(req.project_id)
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

    cmd = [
        sys.executable, "-m", "agents.web_collector",
        "--project", project_id,
        "--domain", domain,
        "--task-type", task_type,
        "--topic", topic,
        "--max-pairs", str(max_pairs),
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
        records = [r for r in records if r.get("review_status") not in ("validated", "rejected")]
    elif status in ("validated", "rejected"):
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

    return {
        "project_id": project_id,
        "funnel": funnel,
        "stages": {
            "raw": raw,
            "processed": processed,
            "fine_tuning_ready": {**ftr, **splits},
        },
        "alerts": alerts,
    }
