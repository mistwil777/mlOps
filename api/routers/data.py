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


def _all_raw_records(project_id: str, stage: str = "raw") -> list[dict]:
    """Aggregate records from all JSONL files in a stage directory."""
    stage_dir = Path(f"datalake/projects/{project_id}/{stage}")
    if not stage_dir.exists():
        return []
    records: list[dict] = []
    pairs_raw = stage_dir / "pairs_raw.jsonl"
    for f in sorted(stage_dir.glob("*.jsonl")):
        if f.is_file() and f != pairs_raw:
            records.extend(_list_jsonl(f))
    return records


def _pair_path(project_id: str, stage: str) -> Path:
    _validate_project_id(project_id)
    return Path(f"datalake/projects/{project_id}/{stage}/pairs_raw.jsonl")


def _record_key(r: dict[str, Any]) -> tuple:
    """Stable dedup key for a raw collector record."""
    return (
        r.get("source_url", ""),
        r.get("task_name", ""),
        r.get("lang", ""),
        r.get("description", "")[:80],
        r.get("code", "")[:80],
        r.get("prompt", "")[:80],
    )


def _ensure_raw_consolidated(project_id: str, stage: str = "raw") -> Path:
    """Return consolidated.jsonl path, merging all collector *.jsonl into it as-is.

    Records are stored in their native schema (description/code, task_name/code, prompt/completion).
    No transformation to prompt/completion at this stage — that happens in pipeline/transform.py.
    """
    stage_dir = Path(f"datalake/projects/{project_id}/{stage}")
    _validate_project_id(project_id)
    if not stage_dir.exists():
        stage_dir.mkdir(parents=True, exist_ok=True)

    consolidated = stage_dir / "consolidated.jsonl"
    source_files = sorted(f for f in stage_dir.glob("*.jsonl") if f != consolidated and f.is_file())
    if not source_files:
        return consolidated

    existing = _list_jsonl(consolidated)
    existing_keys: set[tuple] = {_record_key(r) for r in existing}

    new_records: list[dict] = []
    for f in source_files:
        for r in _list_jsonl(f):
            k = _record_key(r)
            if k not in existing_keys:
                new_records.append(r)
                existing_keys.add(k)

    if new_records:
        _write_jsonl(consolidated, existing + new_records)

    return consolidated


# Keep old name as alias for review write-back compatibility
def _ensure_pairs_raw(project_id: str, stage: str = "raw") -> Path:
    return _ensure_raw_consolidated(project_id, stage)


# ── AWA Collect ───────────────────────────────────────────────────────────────

_COLLECTOR_REGISTRY: dict[str, Any] | None = None


def _load_collector_registry() -> dict[str, Any]:
    global _COLLECTOR_REGISTRY
    if _COLLECTOR_REGISTRY is None:
        path = Path("config/collectors/registry.yaml")
        if not path.exists():
            raise HTTPException(500, "config/collectors/registry.yaml not found")
        with path.open() as fh:
            data = yaml.safe_load(fh) or {}
        _COLLECTOR_REGISTRY = data.get("collectors", {})
    return _COLLECTOR_REGISTRY


class CollectRequest(BaseModel):
    project_id: str
    topic: Optional[str] = None
    max_pairs: int = 50
    domain: Optional[str] = None
    task_type: Optional[str] = None
    collector_type: str = "web_crawl"   # must match an entry in config/collectors/registry.yaml
    source_value: Optional[str] = None  # collector-specific param (e.g. seed URL or topic)
    adapter_name: Optional[str] = None  # which adapter these pairs belong to
    langs: Optional[list[str]] = None   # for multilang_wiki: e.g. ["Python", "Java"]
    lang: Optional[str] = None          # for doc_crawl: "python" | "java" | …
    custom_tags: Optional[list[str]] = None  # for doc_crawl: custom HTML tags
    max_depth: Optional[int] = None     # for doc_crawl: crawl depth override


@router.post("/collect")
async def start_collect(req: CollectRequest) -> dict[str, str]:
    registry = _load_collector_registry()
    if req.collector_type not in registry:
        known = sorted(registry.keys())
        raise HTTPException(
            400,
            f"Unknown collector_type '{req.collector_type}'. "
            f"Known types: {known}. Add a new entry in config/collectors/registry.yaml."
        )

    try:
        config = _load_project_config(req.project_id)
    except HTTPException:
        config = {}
    if req.domain:
        config.setdefault("domain", req.domain)
    if req.task_type:
        config.setdefault("task_type", req.task_type)

    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {
        "status":     "running",
        "lines":      [],
        "project_id": req.project_id,
    }

    asyncio.create_task(_run_collect(
        job_id, req.project_id, req.topic, req.max_pairs, config,
        req.collector_type, req.source_value, req.adapter_name,
        req.langs, req.lang, req.custom_tags, req.max_depth,
    ))
    return {"job_id": job_id}


_COLLECT_TIMEOUT_S = 1800  # 30 min — doc crawls can visit 400+ pages at ~1s each


async def _run_collect(
    job_id: str,
    project_id: str,
    topic: str | None,
    max_pairs: int,
    config: dict[str, Any],
    collector_type: str = "web_crawl",
    source_value: str | None = None,
    adapter_name: str | None = None,
    langs: list[str] | None = None,
    lang: str | None = None,
    custom_tags: list[str] | None = None,
    max_depth: int | None = None,
) -> None:
    registry  = _load_collector_registry()
    collector = registry[collector_type]
    module    = collector.get("module")

    # Output path: timestamped per-collector file — never overwrite a previous run
    raw_dir = Path(f"datalake/projects/{project_id}/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    output = raw_dir / f"{collector_type}_{ts}.jsonl"

    adapter_args = ["--adapter-name", adapter_name] if adapter_name else []

    # Build args from CLI mapping + request values
    cli_map: dict[str, str] = collector.get("cli_mapping", {})
    extra_args: list[str] = []

    # Auto-derive langs for multilang_wiki if not explicitly provided
    effective_langs = langs
    if effective_langs is None and collector_type == "multilang_wiki":
        transform = config.get("transform", {})
        params = transform.get("params", {})
        src = params.get("src_lang") or params.get("source_lang")
        tgt = params.get("tgt_lang") or params.get("target_lang")
        if src and tgt:
            effective_langs = [src, tgt]
        else:
            # Fallback 1: fusible doc_crawl lang_hints across all adapters
            hints: list[str] = []
            for adapter_cfg in config.get("adapters", []):
                for sc in adapter_cfg.get("source_configs", []):
                    if sc.get("fusible") and sc.get("lang_hint"):
                        h = sc["lang_hint"]
                        if h not in hints:
                            hints.append(h)
            # Fallback 2: any lang_hint across source_configs if no fusible found
            if not hints:
                for adapter_cfg in config.get("adapters", []):
                    for sc in adapter_cfg.get("source_configs", []):
                        h = sc.get("lang_hint", "")
                        if h and h not in hints:
                            hints.append(h)
            if hints:
                effective_langs = hints

    # Map known request fields to their CLI arg names
    field_values: dict[str, Any] = {
        "topic":       source_value or topic or None,
        "url":         source_value or topic or None,
        "max_tasks":   max_pairs,
        "max_pages":   max_pairs,
        "max_depth":   max_depth,
        "lang":        lang or topic or None,
        "langs":       effective_langs,
        "custom_tags": custom_tags,
        "domain":      config.get("domain", "code"),
        "task_type":   config.get("task_type", "generation"),
        "project":     project_id,
    }

    for field, cli_arg in cli_map.items():
        if field == "adapter_name":
            continue  # handled via adapter_args
        val = field_values.get(field)
        if val is None:
            continue
        if isinstance(val, list):
            if val:
                extra_args += [cli_arg] + [str(v) for v in val]
        else:
            extra_args += [cli_arg, str(val)]

    if module is None:
        # manual collector — no subprocess, nothing to launch
        _JOBS[job_id]["status"] = "completed"
        _JOBS[job_id]["lines"].append("Manual collector: no subprocess launched.")
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        return

    cmd = [sys.executable, "-m", module, "--output", str(output), *extra_args, *adapter_args]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def _read_stdout() -> None:
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                _JOBS[job_id]["lines"].append(line)
            await proc.wait()

        await asyncio.wait_for(_read_stdout(), timeout=_COLLECT_TIMEOUT_S)
        status = "completed" if proc.returncode == 0 else "failed"
        _JOBS[job_id]["status"] = status
        _JOBS[job_id]["returncode"] = proc.returncode
        if status == "completed":
            asyncio.create_task(_index_datalake_pairs(project_id, job_id=job_id, stage="raw"))
    except asyncio.TimeoutError:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: collect timed out after {_COLLECT_TIMEOUT_S}s")
        proc.kill()
        await proc.wait()
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


# ── Pair Fusion ───────────────────────────────────────────────────────────────

class FuseRequest(BaseModel):
    project_id: str
    lang_pair: str          # key from config/languages.yaml, e.g. "python-java"
    adapter_name: Optional[str] = None
    max_fused: int = 200
    llm_model: str = "qwen2.5-coder:7b"


@router.post("/fuse")
async def start_fuse(req: FuseRequest) -> dict[str, str]:
    _load_project_config(req.project_id)
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id}
    asyncio.create_task(_run_fuse(job_id, req))
    return {"job_id": job_id}


async def _run_fuse(job_id: str, req: FuseRequest) -> None:
    raw_dir = Path(f"datalake/projects/{req.project_id}/raw")

    # Load language pair config
    try:
        import yaml as _yaml  # noqa: PLC0415
        with open("config/languages.yaml") as fh:
            langs_cfg = _yaml.safe_load(fh) or {}
        pcfg = langs_cfg.get("pairs", {}).get(req.lang_pair)
        if not pcfg:
            _JOBS[job_id]["status"] = "failed"
            _JOBS[job_id]["lines"].append(f"ERROR: unknown lang_pair '{req.lang_pair}'")
            return
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR loading languages.yaml: {exc}")
        return

    source_lang = pcfg["source_lang"]
    target_lang = pcfg["target_lang"]

    # Merge all doc_crawl_*.jsonl + multilang_wiki_*.jsonl files into two lang-filtered files
    # that pair_fuser expects: one per lang.
    import json as _json  # noqa: PLC0415
    source_jsonl = raw_dir / f"_fuse_src_{source_lang}.jsonl"
    target_jsonl = raw_dir / f"_fuse_tgt_{target_lang}.jsonl"
    output       = raw_dir / "pairs_raw.jsonl"

    all_raw: list[dict] = []
    for pattern in ("doc_crawl_*.jsonl", "multilang_wiki_*.jsonl"):
        for f in sorted(raw_dir.glob(pattern)):
            try:
                for line in f.read_text().splitlines():
                    if line.strip():
                        all_raw.append(_json.loads(line))
            except Exception:
                pass

    src_records = [r for r in all_raw if r.get("lang", "").lower() == source_lang.lower()]
    tgt_records = [r for r in all_raw if r.get("lang", "").lower() == target_lang.lower()]

    _JOBS[job_id]["lines"].append(
        f"Collected {len(src_records)} {source_lang} + {len(tgt_records)} {target_lang} records from raw files"
    )

    source_jsonl.write_text("\n".join(_json.dumps(r) for r in src_records))
    target_jsonl.write_text("\n".join(_json.dumps(r) for r in tgt_records))

    cmd = [
        sys.executable, "-m", "agents.pair_fuser",
        "--pair",         req.lang_pair,
        "--source-jsonl", str(source_jsonl),
        "--target-jsonl", str(target_jsonl),
        "--output",       str(output),
        "--max-fused",    str(req.max_fused),
        "--model",        req.llm_model,
        *(["--adapter-name", req.adapter_name] if req.adapter_name else []),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def _read() -> None:
            async for raw in proc.stdout:
                _JOBS[job_id]["lines"].append(raw.decode(errors="replace").rstrip())
            await proc.wait()

        await asyncio.wait_for(_read(), timeout=1800)  # 30 min max
        _JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
    except asyncio.TimeoutError:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append("ERROR: fuse timed out after 30 min")
        proc.kill()
        await proc.wait()
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        for tmp in (source_jsonl, target_jsonl):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        _purge_old_jobs()


# ── Human Review ──────────────────────────────────────────────────────────────

class ReviewAction(BaseModel):
    index: int
    action: str                              # "flag" | "rescue"
    corrections: dict[str, str] | None = None  # field → corrected value (any native schema field)
    note: str | None = None                  # human note attached to the flag


# ── Human Review — pipeline-aware ────────────────────────────────────────────
# Philosophy: humans do NOT validate every record. They:
#   1. Spot-check a sample of qualified records (sanity check)
#   2. Review auto-rejected records to rescue false positives
#   3. Flag records that slipped through but are clearly wrong

def _processed_dir(project_id: str) -> Path:
    return Path(f"datalake/projects/{project_id}/processed")


@router.get("/review/{project_id}/quality-summary")
async def review_quality_summary(project_id: str) -> dict[str, Any]:
    """Pipeline quality stats: how many records at each stage, rejection breakdown per step."""
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    proc = _processed_dir(project_id)
    qualified_path = proc / "qualified.jsonl"
    qualified = _list_jsonl(qualified_path) if qualified_path.exists() else []

    # Per-step rejection files
    step_names = ["schema", "syntax", "text_quality", "dedup", "relevance"]
    rejection_counts: dict[str, int] = {}
    for step in step_names:
        p = proc / f"rejected_{step}.jsonl"
        rejection_counts[step] = len(_list_jsonl(p)) if p.exists() else 0

    # Flagged by human
    flagged_path = proc / "flagged.jsonl"
    flagged = len(_list_jsonl(flagged_path)) if flagged_path.exists() else 0

    # Rescued false positives
    rescued_path = proc / "rescued.jsonl"
    rescued = len(_list_jsonl(rescued_path)) if rescued_path.exists() else 0

    # Raw total
    raw_path = _ensure_pairs_raw(project_id, "raw")
    raw_total = len(_list_jsonl(raw_path))

    total_rejected = sum(rejection_counts.values())

    return {
        "project_id":      project_id,
        "raw_total":       raw_total,
        "auto_rejected":   total_rejected,
        "qualified":       len(qualified),
        "flagged_by_human": flagged,
        "rescued":         rescued,
        "rejection_by_step": rejection_counts,
        "pipeline_ran":    qualified_path.exists(),
    }


@router.get("/review/{project_id}/rejected")
async def list_rejected(
    project_id: str,
    step: str | None = None,     # schema | syntax | text_quality | dedup | relevance | all
    page: int = 0,
    page_size: int = 20,
) -> dict[str, Any]:
    """Browse auto-rejected records. Humans look here to rescue false positives."""
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    proc = _processed_dir(project_id)
    step_names = ["schema", "syntax", "text_quality", "dedup", "relevance"]

    if step and step != "all" and step not in step_names:
        raise HTTPException(400, f"Unknown step '{step}'. Valid: {step_names + ['all']}")

    target_steps = step_names if (not step or step == "all") else [step]
    records: list[dict] = []
    for s in target_steps:
        p = proc / f"rejected_{s}.jsonl"
        if p.exists():
            for r in _list_jsonl(p):
                records.append({**r, "_rejected_step": s})

    total  = len(records)
    sliced = records[page * page_size: (page + 1) * page_size]
    return {
        "total": total, "page": page, "page_size": page_size,
        "step": step or "all", "items": sliced,
    }


@router.get("/review/{project_id}/sample")
async def review_sample(
    project_id: str,
    n: int = 20,
    adapter_name: str | None = None,
) -> dict[str, Any]:
    """Random sample of qualified records for human spot-check."""
    import random  # noqa: PLC0415
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    qualified_path = _processed_dir(project_id) / "qualified.jsonl"
    if not qualified_path.exists():
        return {"total": 0, "sample": [], "note": "Quality pipeline not yet run."}

    records = _list_jsonl(qualified_path)
    if adapter_name:
        records = [r for r in records if r.get("adapter_name") == adapter_name]

    sample = random.sample(records, min(n, len(records)))
    return {"total": len(records), "sample_size": len(sample), "sample": sample}


@router.post("/review/{project_id}/flag")
async def flag_record(project_id: str, action: ReviewAction) -> dict[str, Any]:
    """Human flags a qualified record as wrong (false positive that slipped through)."""
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    qualified_path = _processed_dir(project_id) / "qualified.jsonl"
    if not qualified_path.exists():
        raise HTTPException(404, "No qualified records found. Run the quality pipeline first.")

    records = _list_jsonl(qualified_path)
    if action.index < 0 or action.index >= len(records):
        raise HTTPException(400, f"Index {action.index} out of range (total={len(records)})")

    r = dict(records[action.index])
    r["_human_flag"] = True
    r["_human_note"] = action.note or ""
    r["_flagged_at"]  = datetime.now(timezone.utc).isoformat()

    # Remove from qualified, add to flagged
    records.pop(action.index)
    _write_jsonl(qualified_path, records)

    flagged_path = _processed_dir(project_id) / "flagged.jsonl"
    existing_flagged = _list_jsonl(flagged_path)
    _write_jsonl(flagged_path, existing_flagged + [r])

    return {"ok": True, "index": action.index, "flagged_total": len(existing_flagged) + 1}


@router.post("/review/{project_id}/rescue")
async def rescue_record(project_id: str, action: ReviewAction) -> dict[str, Any]:
    """Human rescues a false-positive auto-rejected record, optionally correcting fields."""
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    proc = _processed_dir(project_id)
    step_names = ["schema", "syntax", "text_quality", "dedup", "relevance"]

    # Find the record across all rejection files
    target_step: str | None = None
    target_local_idx: int | None = None
    cumulative = 0
    for step in step_names:
        p = proc / f"rejected_{step}.jsonl"
        if not p.exists():
            continue
        recs = _list_jsonl(p)
        end = cumulative + len(recs)
        if cumulative <= action.index < end:
            target_step = step
            target_local_idx = action.index - cumulative
            break
        cumulative = end

    if target_step is None or target_local_idx is None:
        raise HTTPException(400, f"Index {action.index} not found across rejection files")

    p = proc / f"rejected_{target_step}.jsonl"
    recs = _list_jsonl(p)
    r = dict(recs[target_local_idx])

    # Apply corrections (any native field)
    if action.corrections:
        for field, value in action.corrections.items():
            r[field] = value
    r["_rescued_from_step"] = target_step
    r["_rescued_at"]        = datetime.now(timezone.utc).isoformat()
    r["_human_note"]        = action.note or ""
    # Remove internal rejection markers
    for k in list(r.keys()):
        if k.startswith("_") and "reject" in k:
            del r[k]

    # Remove from rejection file
    recs.pop(target_local_idx)
    _write_jsonl(p, recs)

    # Add to rescued + qualified
    rescued_path = proc / "rescued.jsonl"
    _write_jsonl(rescued_path, _list_jsonl(rescued_path) + [r])

    qualified_path = proc / "qualified.jsonl"
    _write_jsonl(qualified_path, _list_jsonl(qualified_path) + [r])

    return {"ok": True, "rescued_from": target_step, "record": r}


@router.get("/review/{project_id}/stats")
async def review_stats(project_id: str) -> dict[str, Any]:
    """Legacy stats endpoint — redirects to quality-summary for backwards compat."""
    return await review_quality_summary(project_id)


# ── Cleaning Pipeline ─────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    project_id: str
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
        "--score", "false",
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
        if proc.returncode == 0:
            _JOBS[job_id]["status"] = "completed"
            asyncio.create_task(_index_datalake_pairs(req.project_id, job_id))
        else:
            _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["returncode"] = proc.returncode
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        _purge_old_jobs()


async def _index_datalake_pairs(project_id: str, job_id: str | None = None, stage: str | None = None) -> None:
    from pipeline.datalake_index import DatalakeIndex  # noqa: PLC0415
    stages = [stage] if stage else ["raw", "fine-tuning-ready"]
    try:
        idx = await asyncio.get_event_loop().run_in_executor(None, DatalakeIndex)
        total = 0
        for s in stages:
            n = await asyncio.get_event_loop().run_in_executor(None, lambda s=s: idx.index_project(project_id, stage=s))
            total += n
        msg = f"[datalake-index] Indexed {total} pairs for {project_id} (stages: {', '.join(stages)})"
        if job_id and job_id in _JOBS:
            _JOBS[job_id]["lines"].append(msg)
    except Exception as exc:
        hint = " (Qdrant not running — start with: docker compose up qdrant -d)" if "Connection refused" in str(exc) else ""
        msg = f"[datalake-index] Skipped — Qdrant unavailable{hint}"
        if job_id and job_id in _JOBS:
            _JOBS[job_id]["lines"].append(msg)


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

    def _stage_info(stage: str) -> dict[str, Any]:
        stage_dir = base / stage
        if not stage_dir.exists():
            return {"exists": False, "count": 0, "size_mb": 0,
                    "avg_quality": None, "scored_count": 0, "reviewed_count": 0}
        # Aggregate all JSONL files in the stage directory (agnostic — no hardcoded filename)
        jsonl_files = [f for f in stage_dir.glob("*.jsonl") if f.is_file()]
        records: list[dict] = []
        total_bytes = 0
        for f in jsonl_files:
            records.extend(_list_jsonl(f))
            total_bytes += f.stat().st_size
        if not records:
            return {"exists": False, "count": 0, "size_mb": 0,
                    "avg_quality": None, "scored_count": 0, "reviewed_count": 0}
        size_mb = round(total_bytes / 1e6, 2)
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
    ftr       = _stage_info("fine-tuning-ready")  # counts from pipeline output files only
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


# ── Adapter Coverage ──────────────────────────────────────────────────────────

@router.get("/adapter-coverage/{project_id}")
async def adapter_coverage(project_id: str) -> dict[str, Any]:
    """Return per-adapter record counts vs targets, read directly from raw JSONL."""
    cfg = _load_project_config(project_id)
    adapters: list[dict[str, Any]] = cfg.get("adapters", [])

    records = _all_raw_records(project_id, "raw")

    counts: dict[str, int] = {}
    untagged = 0
    for r in records:
        name = r.get("adapter_name")
        if name:
            counts[name] = counts.get(name, 0) + 1
        else:
            untagged += 1

    coverage = []
    total_target = 0
    total_collected = 0
    for a in adapters:
        name = a["name"]
        target = a.get("estimated_pairs", 0)
        collected = counts.get(name, 0)
        missing = max(0, target - collected)
        pct = round(collected / target * 100) if target > 0 else 0
        total_target += target
        total_collected += collected
        coverage.append({
            "adapter_name":  name,
            "role":          a.get("role", "specialist"),
            "target":        target,
            "collected":     collected,
            "missing":       missing,
            "pct":           pct,
            "source_configs": a.get("source_configs", []),
        })

    return {
        "project_id":      project_id,
        "total_target":    total_target,
        "total_collected": total_collected,
        "total_raw":       len(records),
        "untagged":        untagged,
        "adapters":        coverage,
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

    # Accept any native schema — no prompt/completion requirement at raw stage
    # Append to a timestamped file to avoid overwriting previous ingests
    ingest_ts = datetime.now(timezone.utc)
    ingest_file = raw_dir / f"manual_{ingest_ts.strftime('%Y%m%dT%H%M%S')}.jsonl"
    ts_iso = ingest_ts.isoformat()
    tagged = [{**r, "meta": {**r.get("meta", {}), "source": source_description, "ingested_at": ts_iso}} for r in records_new]
    _write_jsonl(ingest_file, tagged)
    existing = _list_jsonl(raw_path)

    # Track lineage
    for r in tagged:
        _track_lineage(project_id, r, source_description)

    return {
        "ingested": len(tagged),
        "file": ingest_file.name,
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
    # Migration: add adapter_name column if missing
    try:
        execute("ALTER TABLE data_lineage ADD COLUMN adapter_name TEXT")
    except Exception:
        pass


def _track_lineage(project_id: str, record: dict[str, Any], source_type: str) -> None:
    _ensure_lineage_table()
    try:
        meta = record.get("meta", {})
        prompt_tokens = len(record.get("prompt", "").split())
        completion_tokens = len(record.get("completion", "").split())
        adapter_name = record.get("adapter_name") or meta.get("adapter_name")
        execute(
            "INSERT OR IGNORE INTO data_lineage (id,project_id,source_type,source_url,collected_at,quality_score,review_status,prompt_tokens,completion_tokens,adapter_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                project_id,
                source_type,
                record.get("source_url") or meta.get("source_url", ""),
                record.get("collected_at") or meta.get("ingested_at") or datetime.now(timezone.utc).isoformat(),
                record.get("quality_score"),
                record.get("review_status"),
                prompt_tokens,
                completion_tokens,
                adapter_name,
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


# ── Datalake Search ────────────────────────────────────────────────────────────

class SearchPairsRequest(BaseModel):
    query: str
    project_id: Optional[str] = None
    stage: Optional[str] = None         # "raw" | "fine-tuning-ready"
    adapter_name: Optional[str] = None  # filter by adapter
    k: int = 20
    page: int = 0


@router.post("/search")
async def search_pairs(body: SearchPairsRequest) -> dict[str, Any]:
    from pipeline.datalake_index import DatalakeIndex  # noqa: PLC0415
    loop = asyncio.get_event_loop()
    try:
        idx = await loop.run_in_executor(None, DatalakeIndex)
        results = await loop.run_in_executor(
            None,
            lambda: idx.search(
                body.query,
                project_id=body.project_id or None,
                stage=body.stage or None,
                adapter_name=body.adapter_name or None,
                k=body.k,
            ),
        )
    except Exception as exc:
        raise HTTPException(503, f"Datalake index unavailable: {exc}")

    offset = body.page * body.k
    page_items = results[offset: offset + body.k] if offset < len(results) else results

    return {
        "total": len(results),
        "page": body.page,
        "page_size": body.k,
        "items": page_items,
    }


@router.get("/browse/{project_id}")
async def browse_pairs(
    project_id: str,
    stage: str = "raw",
    adapter_name: Optional[str] = None,
    page: int = 0,
    page_size: int = 20,
) -> dict[str, Any]:
    """Browse raw JSONL pairs directly — no Qdrant needed. Supports adapter_name filter."""
    _load_project_config(project_id)
    raw_path = _ensure_pairs_raw(project_id, stage)
    records = _list_jsonl(raw_path)

    if adapter_name:
        records = [r for r in records if r.get("adapter_name") == adapter_name]

    total = len(records)
    sliced = records[page * page_size: (page + 1) * page_size]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "adapter_name": adapter_name,
        "stage": stage,
        "items": sliced,
    }


@router.post("/index-project/{project_id}")
async def index_project_pairs(project_id: str, stage: str | None = None) -> dict[str, Any]:
    asyncio.create_task(_index_datalake_pairs(project_id, stage=stage))
    return {"ok": True, "project_id": project_id}
