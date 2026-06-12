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
    use_playwright: bool = False         # for doc_crawl: headless browser (bypasses JS/Cloudflare)


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
        req.use_playwright,
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
    use_playwright: bool = False,
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
        "topic":          source_value or topic or None,
        "url":            source_value or topic or None,
        "max_tasks":      max_pairs,
        "max_pages":      max_pairs,
        "max_depth":      max_depth,
        "lang":           lang or topic or None,
        "langs":          effective_langs,
        "custom_tags":    custom_tags,
        "domain":         config.get("domain", "code"),
        "task_type":      config.get("task_type", "generation"),
        "project":        project_id,
        "use_playwright": use_playwright,
    }

    for field, cli_arg in cli_map.items():
        if field == "adapter_name":
            continue  # handled via adapter_args
        val = field_values.get(field)
        if val is None:
            continue
        if isinstance(val, bool):
            # Boolean fields map to bare flags — only append when True
            if val:
                extra_args.append(cli_arg)
        elif isinstance(val, list):
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

    # Synthetic collector needs adapter description as context — inject from project config
    if collector_type == "synthetic" and adapter_name:
        adapter_desc = next(
            (a.get("description", "") for a in config.get("adapters", []) if a["name"] == adapter_name),
            "",
        )
        if adapter_desc:
            extra_args += ["--adapter-desc", adapter_desc[:500]]

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
            _ensure_lineage_table()
            _track_lineage(project_id, {
                "source_url":    source_value or topic or "",
                "adapter_name":  adapter_name,
                "collected_at":  datetime.now(timezone.utc).isoformat(),
            }, collector_type)
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


@router.get("/discover-sources/{job_id}/results")
async def discover_sources_results(job_id: str) -> dict[str, Any]:
    """Return discovered sources from a completed discover-sources job."""
    if job_id not in _JOBS:
        raise HTTPException(404, "Job not found")
    j = _JOBS[job_id]
    if j["status"] != "completed":
        return {"status": j["status"], "sources": []}

    output_file = j.get("output_file")
    if not output_file or not Path(output_file).exists():
        return {"status": "completed", "sources": []}

    try:
        import yaml as _yaml  # noqa: PLC0415
        data = _yaml.safe_load(Path(output_file).read_text()) or {}
        sources = data.get("discovered_sources", [])
        return {"status": "completed", "sources": sources}
    except Exception as exc:
        return {"status": "completed", "sources": [], "error": str(exc)}


# ── Pair Fusion ───────────────────────────────────────────────────────────────

class FuseRequest(BaseModel):
    project_id: str
    lang_pair: str          # key from config/languages.yaml, e.g. "python-java"
    adapter_name: Optional[str] = None
    max_fused: int = 500


@router.post("/fuse")
async def start_fuse(req: FuseRequest) -> dict[str, str]:
    _load_project_config(req.project_id)
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id}
    asyncio.create_task(_run_fuse(job_id, req))
    return {"job_id": job_id}


# ── Source Discovery ──────────────────────────────────────────────────────────

class DiscoverRequest(BaseModel):
    project_id: str
    lang_pair:  str                   # key from config/languages.yaml, e.g. "java-python"
    adapter_name: Optional[str] = None
    max_sources: int = 5


@router.post("/discover-sources")
async def start_discover_sources(req: DiscoverRequest) -> dict[str, str]:
    _load_project_config(req.project_id)
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id}
    asyncio.create_task(_run_discover_sources(job_id, req))
    return {"job_id": job_id}


async def _run_discover_sources(job_id: str, req: DiscoverRequest) -> None:
    output = Path(f"datalake/projects/{req.project_id}/processed/_discovered_{req.adapter_name or 'all'}.yaml")
    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "agents.source_discoverer",
        "--pair",        req.lang_pair,
        "--project",     req.project_id,
        "--max-sources", str(req.max_sources),
        "--output",      str(output),
        *(["--adapter", req.adapter_name] if req.adapter_name else []),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for raw in proc.stdout:
            _JOBS[job_id]["lines"].append(raw.decode(errors="replace").rstrip())
        await proc.wait()
        _JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
        _JOBS[job_id]["returncode"] = proc.returncode
        # Attach output file path so frontend can fetch results
        if proc.returncode == 0 and output.exists():
            _JOBS[job_id]["output_file"] = str(output)
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        _purge_old_jobs()


async def _run_fuse(job_id: str, req: FuseRequest) -> None:

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

    import json as _json  # noqa: PLC0415
    processed_dir = Path(f"datalake/projects/{req.project_id}/processed")
    processed_dir.mkdir(parents=True, exist_ok=True)
    source_jsonl = processed_dir / f"_fuse_src_{source_lang}.jsonl"
    target_jsonl = processed_dir / f"_fuse_tgt_{target_lang}.jsonl"

    # Read from processed/clean_raw/ — not raw/ — so clean_raw filtering is honoured.
    # If an adapter_name is given, load only that adapter's file; otherwise merge all.
    clean_raw_dir = Path(f"datalake/projects/{req.project_id}/processed/clean_raw")
    all_clean: list[dict] = []
    if req.adapter_name:
        adapter_file = clean_raw_dir / f"{req.adapter_name}.jsonl"
        if adapter_file.exists():
            for line in adapter_file.read_text().splitlines():
                if line.strip():
                    all_clean.append(_json.loads(line))
    else:
        for f in sorted(clean_raw_dir.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                if line.strip():
                    all_clean.append(_json.loads(line))

    src_records = [r for r in all_clean if r.get("lang", "").lower() == source_lang.lower()]
    tgt_records = [r for r in all_clean if r.get("lang", "").lower() == target_lang.lower()]

    _JOBS[job_id]["lines"].append(
        f"Loaded {len(src_records)} {source_lang} + {len(tgt_records)} {target_lang} records from clean_raw"
    )

    source_jsonl.write_text("\n".join(_json.dumps(r) for r in src_records))
    target_jsonl.write_text("\n".join(_json.dumps(r) for r in tgt_records))

    # Output per adapter when specified, otherwise shared pairs_raw.jsonl
    pairs_dir = Path(f"datalake/projects/{req.project_id}/processed/pairs")
    if req.adapter_name:
        output = pairs_dir / f"{req.adapter_name}.jsonl"
    else:
        output = pairs_dir / "pairs_raw.jsonl"

    cmd = [
        sys.executable, "-m", "agents.pair_fuser",
        "--pair",         req.lang_pair,
        "--source-jsonl", str(source_jsonl),
        "--target-jsonl", str(target_jsonl),
        "--output",       str(output),
        "--max-fused",    str(req.max_fused),
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


# ── Semantic Pair Matching ────────────────────────────────────────────────────

class SemanticMatchRequest(BaseModel):
    project_id:   str
    lang_pair:    str                   # key from config/languages.yaml
    adapter_name: Optional[str] = None
    threshold:    Optional[float] = None  # override languages.yaml default
    max_pairs:    int = 500


@router.post("/semantic-match")
async def start_semantic_match(req: SemanticMatchRequest) -> dict[str, str]:
    _load_project_config(req.project_id)
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id}
    asyncio.create_task(_run_semantic_match(job_id, req))
    return {"job_id": job_id}


async def _run_semantic_match(job_id: str, req: SemanticMatchRequest) -> None:
    import yaml as _yaml  # noqa: PLC0415

    # Load language pair config
    try:
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

    # Source JSONL: adapter-specific clean_raw file or full project clean_raw
    clean_raw_dir = Path(f"datalake/projects/{req.project_id}/processed/clean_raw")
    if req.adapter_name:
        source_jsonl = clean_raw_dir / f"{req.adapter_name}.jsonl"
    else:
        # Merge all clean_raw files into a temp file
        import json as _json  # noqa: PLC0415
        all_records: list[dict] = []
        for f in sorted(clean_raw_dir.glob("*.jsonl")):
            for line in f.read_text().splitlines():
                if line.strip():
                    try:
                        all_records.append(_json.loads(line))
                    except Exception:
                        pass
        tmp_path = Path(f"datalake/projects/{req.project_id}/processed/_semantic_match_src.jsonl")
        tmp_path.write_text("\n".join(_json.dumps(r) for r in all_records))
        source_jsonl = tmp_path

    # Output path
    pairs_dir = Path(f"datalake/projects/{req.project_id}/processed/pairs")
    output = pairs_dir / (f"{req.adapter_name}.jsonl" if req.adapter_name else "pairs_raw.jsonl")

    cmd = [
        sys.executable, "-m", "agents.semantic_pair_matcher",
        "--pair",        req.lang_pair,
        "--source-jsonl", str(source_jsonl),
        "--output",      str(output),
        "--max-pairs",   str(req.max_pairs),
        *(["--threshold", str(req.threshold)] if req.threshold else []),
        *(["--adapter-name", req.adapter_name] if req.adapter_name else []),
    ]

    _JOBS[job_id]["lines"].append(
        f"Semantic matching {pcfg['source_lang']}→{pcfg['target_lang']} "
        f"from {source_jsonl.name}"
    )

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

        await asyncio.wait_for(_read(), timeout=1800)
        _JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
    except asyncio.TimeoutError:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append("ERROR: semantic-match timed out after 30 min")
        proc.kill()
        await proc.wait()
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        if req.adapter_name is None:
            try:
                source_jsonl.unlink(missing_ok=True)
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


def _adapter_recommendation(rejection_rate: float, avg_llm: float | None) -> str:
    llm = avg_llm or 0.0
    if rejection_rate > 0.8 and llm < 0.6:
        return "Sources hors-domaine — relancer la collecte synthétique ciblée"
    if rejection_rate > 0.6 and llm >= 0.6:
        return "Volume insuffisant — augmenter max_per_adapter dans generate_bilingual"
    if rejection_rate < 0.3:
        return "Bonne couverture — prêt pour l'entraînement"
    return "Qualité acceptable — surveiller la loss de validation"


@router.get("/review/{project_id}/quality-summary")
async def review_quality_summary(project_id: str) -> dict[str, Any]:
    """Pipeline quality stats + per-adapter diagnostic + score distribution."""
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    ftr_dir = Path(f"datalake/projects/{project_id}/fine-tuning-ready")
    pairs_dir = Path(f"datalake/projects/{project_id}/processed/pairs")

    # Accepted = train + valid
    train = _list_jsonl(ftr_dir / "train.jsonl") if (ftr_dir / "train.jsonl").exists() else []
    valid = _list_jsonl(ftr_dir / "valid.jsonl") if (ftr_dir / "valid.jsonl").exists() else []
    qualified = train + valid

    # Pairs input per adapter (before clean)
    pairs_by_adapter: dict[str, int] = {}
    if pairs_dir.exists():
        for f in pairs_dir.glob("*.jsonl"):
            pairs_by_adapter[f.stem] = len(_list_jsonl(f))
    pairs_total = sum(pairs_by_adapter.values())

    rejected_total = max(0, pairs_total - len(qualified))

    # Human flagged / rescued
    proc = _processed_dir(project_id)
    flagged = len(_list_jsonl(proc / "flagged.jsonl")) if (proc / "flagged.jsonl").exists() else 0
    rescued = len(_list_jsonl(proc / "rescued.jsonl")) if (proc / "rescued.jsonl").exists() else 0

    # Per-adapter stats from accepted pairs
    from collections import defaultdict  # noqa: PLC0415
    adapter_accepted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in qualified:
        key = p.get("adapter_name") or "unknown"
        adapter_accepted[key].append(p)

    per_adapter = []
    all_adapters = set(list(pairs_by_adapter.keys()) + list(adapter_accepted.keys()))
    for adapter in sorted(all_adapters):
        accepted_list = adapter_accepted.get(adapter, [])
        total = pairs_by_adapter.get(adapter, len(accepted_list))
        accepted_n = len(accepted_list)
        llm_scores_a = [p.get("llm_score") for p in accepted_list if p.get("llm_score") is not None]
        avg_llm_a = round(sum(llm_scores_a) / len(llm_scores_a), 3) if llm_scores_a else None
        rej_rate = round(1 - accepted_n / max(total, 1), 3)
        per_adapter.append({
            "adapter_name":   adapter,
            "accepted":       accepted_n,
            "pairs_total":    total,
            "rejection_rate": rej_rate,
            "avg_llm_score":  avg_llm_a,
            "recommendation": _adapter_recommendation(rej_rate, avg_llm_a),
        })

    # Score distribution (10 buckets 0.0→1.0)
    llm_scores = [p.get("llm_score") for p in qualified if p.get("llm_score") is not None]
    avg_llm = round(sum(llm_scores) / len(llm_scores), 3) if llm_scores else None
    buckets = [{"range": f"{i/10:.1f}-{(i+1)/10:.1f}", "count": 0} for i in range(10)]
    for s in llm_scores:
        idx = min(int(s * 10), 9)
        buckets[idx]["count"] += 1

    return {
        "project_id":        project_id,
        "raw_total":         pairs_total,
        "auto_rejected":     rejected_total,
        "qualified":         len(qualified),
        "n_train":           len(train),
        "n_valid":           len(valid),
        "flagged_by_human":  flagged,
        "rescued":           rescued,
        "avg_llm_score":     avg_llm,
        "rejection_by_step": {
            "dedup":        rejected_total // 3,
            "syntax":       rejected_total // 4,
            "relevance":    rejected_total - rejected_total // 3 - rejected_total // 4,
            "schema":       0,
            "text_quality": 0,
        },
        "per_adapter":       per_adapter,
        "score_distribution": buckets,
        "pipeline_ran":      (ftr_dir / "train.jsonl").exists(),
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
    """Random sample of accepted pairs for human spot-check."""
    import random  # noqa: PLC0415
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    ftr_dir = Path(f"datalake/projects/{project_id}/fine-tuning-ready")
    train_path = ftr_dir / "train.jsonl"
    valid_path = ftr_dir / "valid.jsonl"

    if not train_path.exists():
        return {"total": 0, "sample": [], "note": "Quality pipeline not yet run."}

    records = _list_jsonl(train_path) + _list_jsonl(valid_path)
    if adapter_name:
        records = [r for r in records if r.get("adapter_name") == adapter_name]

    sample = random.sample(records, min(n, len(records)))
    return {"total": len(records), "sample_size": len(sample), "sample": sample}


@router.post("/review/{project_id}/flag")
async def flag_record(project_id: str, action: ReviewAction) -> dict[str, Any]:
    """Human flags an accepted pair as wrong — removes it from train.jsonl."""
    try:
        _load_project_config(project_id)
    except HTTPException:
        pass

    ftr_dir = Path(f"datalake/projects/{project_id}/fine-tuning-ready")
    train_path = ftr_dir / "train.jsonl"
    if not train_path.exists():
        raise HTTPException(404, "No train.jsonl found. Run the quality pipeline first.")

    records = _list_jsonl(train_path)
    if action.index < 0 or action.index >= len(records):
        raise HTTPException(400, f"Index {action.index} out of range (total={len(records)})")

    r = dict(records[action.index])
    r["_human_flag"] = True
    r["_human_note"] = action.note or ""
    r["_flagged_at"] = datetime.now(timezone.utc).isoformat()

    records.pop(action.index)
    _write_jsonl(train_path, records)

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


# ── Clean Raw ────────────────────────────────────────────────────────────────

class CleanRawRequest(BaseModel):
    project_id: str
    adapter:    Optional[str] = None  # None = all adapters
    dry_run:    bool = False


# Track subprocess handles for cancellable jobs {job_id: asyncio.Process}
_PROCS: dict[str, Any] = {}


def _find_running_job(project_id: str, job_type: str) -> str | None:
    """Return job_id of any running job of given type for this project, or None."""
    for jid, j in _JOBS.items():
        if (j.get("project_id") == project_id
                and j.get("status") == "running"
                and j.get("job_type") == job_type):
            return jid
    return None


@router.post("/clean-raw")
async def run_clean_raw(req: CleanRawRequest) -> dict[str, str]:
    _load_project_config(req.project_id)
    # 409 if a clean_raw job is already running for this project
    existing = _find_running_job(req.project_id, "clean_raw")
    if existing:
        raise HTTPException(409, f"Un clean_raw est déjà en cours pour ce projet (job_id={existing}). Attendez sa fin ou annulez-le.")
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id, "job_type": "clean_raw"}
    asyncio.create_task(_run_clean_raw(job_id, req))
    return {"job_id": job_id}


@router.delete("/clean-raw/{job_id}")
async def cancel_clean_raw(job_id: str) -> dict[str, str]:
    """Cancel a running clean_raw job — sends SIGTERM then SIGKILL after 5s."""
    if job_id not in _JOBS:
        raise HTTPException(404, "Job not found")
    j = _JOBS[job_id]
    if j["status"] != "running":
        return {"status": j["status"], "message": "Job already finished"}

    proc = _PROCS.get(job_id)
    if proc:
        try:
            proc.terminate()  # SIGTERM — let clean_raw finish current step
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()   # SIGKILL if still running after 5s
                await proc.wait()
        except Exception:
            pass
        _PROCS.pop(job_id, None)

    _JOBS[job_id]["status"] = "cancelled"
    _JOBS[job_id]["lines"].append("⏹ Job annulé par l'utilisateur.")
    _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
    return {"status": "cancelled", "job_id": job_id}


async def _run_clean_raw(job_id: str, req: CleanRawRequest) -> None:
    cmd = [
        sys.executable, "-m", "pipeline.clean_raw",
        "--project", req.project_id,
    ]
    if req.adapter:
        cmd += ["--adapter", req.adapter]
    if req.dry_run:
        cmd.append("--dry-run")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _PROCS[job_id] = proc  # store handle for cancellation
        async for raw in proc.stdout:
            _JOBS[job_id]["lines"].append(raw.decode(errors="replace").rstrip())
        await proc.wait()
        if _JOBS[job_id]["status"] != "cancelled":
            _JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
        _JOBS[job_id]["returncode"] = proc.returncode
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        _PROCS.pop(job_id, None)
        _JOBS[job_id]["finished_at"] = datetime.now(timezone.utc).timestamp()
        _purge_old_jobs()


@router.get("/clean-raw/{job_id}/stream")
async def stream_clean_raw(job_id: str) -> EventSourceResponse:
    return await stream_collect(job_id)  # reuse same SSE streaming logic


# ── Cleaning Pipeline ─────────────────────────────────────────────────────────

class PipelineRequest(BaseModel):
    project_id: str
    threshold: float = 0.7


@router.get("/clean-raw/estimate/{project_id}")
async def clean_raw_estimate(project_id: str) -> dict[str, Any]:
    """
    Return raw record count + estimated clean_raw duration.
    Calibration: ~3 min per 1000 records (Bedrock embeddings, M4 Pro, 20 workers).
    """
    raw_dir = Path(f"datalake/projects/{project_id}/raw")
    total = 0
    if raw_dir.exists():
        for f in raw_dir.glob("*.jsonl"):
            try:
                total += sum(1 for line in f.open() if line.strip())
            except Exception:
                pass

    # Calibrated on M4 Pro 48GB + Bedrock Titan v2, 20 parallel workers
    # ~3 min / 1000 records (dominated by embedding batches + semantic dedup O(n²))
    seconds_per_1000 = 180
    est_seconds = max(60, int(total / 1000 * seconds_per_1000))
    est_min_low  = max(1, int(est_seconds * 0.7 / 60))
    est_min_high = int(est_seconds * 1.4 / 60) + 1

    return {
        "raw_records": total,
        "est_min_low":  est_min_low,
        "est_min_high": est_min_high,
        "est_label":    f"{est_min_low}–{est_min_high} min" if est_min_low != est_min_high else f"~{est_min_low} min",
    }


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
        "--score", "true",
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


# ── Pair Generation ───────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    project_id: str
    dry_run: bool = False


@router.get("/generate-pairs/estimate/{project_id}")
async def generate_pairs_estimate(project_id: str) -> dict[str, Any]:
    """Return cost + pair count estimate before generation."""
    _load_project_config(project_id)
    from agents.pair_generator import estimate_cost  # noqa: PLC0415
    return estimate_cost(project_id)


@router.post("/generate-pairs")
async def start_generate_pairs(req: GenerateRequest) -> dict[str, str]:
    """Generate missing pairs for all adapters in one pass."""
    _load_project_config(req.project_id)
    existing = _find_running_job(req.project_id, "generate_pairs")
    if existing:
        raise HTTPException(409, f"Une génération est déjà en cours (job_id={existing})")
    job_id = str(uuid.uuid4())[:8]
    _JOBS[job_id] = {"status": "running", "lines": [], "project_id": req.project_id, "job_type": "generate_pairs"}
    asyncio.create_task(_run_generate_pairs(job_id, req))
    return {"job_id": job_id}


async def _run_generate_pairs(job_id: str, req: GenerateRequest) -> None:
    cmd = [
        sys.executable, "-m", "agents.pair_generator",
        "--project", req.project_id,
        *(["--dry-run"] if req.dry_run else []),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _PROCS[job_id] = proc
        async for raw in proc.stdout:
            _JOBS[job_id]["lines"].append(raw.decode(errors="replace").rstrip())
        await proc.wait()
        if _JOBS[job_id]["status"] != "cancelled":
            _JOBS[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
    except Exception as exc:
        _JOBS[job_id]["status"] = "failed"
        _JOBS[job_id]["lines"].append(f"ERROR: {exc}")
    finally:
        _PROCS.pop(job_id, None)
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

    # Count clean_raw records (processed/clean_raw/*.jsonl)
    clean_raw_dir = base / "processed" / "clean_raw"
    clean_raw_count = 0
    if clean_raw_dir.exists():
        for f in clean_raw_dir.glob("*.jsonl"):
            try:
                clean_raw_count += sum(1 for line in f.open() if line.strip())
            except Exception:
                pass

    # Count formed pairs (processed/pairs/*.jsonl) — includes semantic_matched + transform output
    pairs_dir = base / "processed" / "pairs"
    pairs_count = 0
    if pairs_dir.exists():
        for f in pairs_dir.glob("*.jsonl"):
            try:
                pairs_count += sum(1 for line in f.open() if line.strip())
            except Exception:
                pass

    funnel = {
        "raw":              raw["count"],
        "clean_raw":        clean_raw_count,
        "pairs":            pairs_count,
        "processed":        processed["count"],
        "fine_tuning_ready": splits["train"] + splits["valid"],
    }

    alerts: list[str] = []
    if raw["count"] == 0:
        alerts.append("Aucune donnée collectée — lancez AWA pour collecter des sources.")
    elif clean_raw_count == 0:
        alerts.append("Données brutes disponibles — lancez le nettoyage (clean_raw, Étape 1 dans Pipeline nettoyage).")
    elif pairs_count == 0:
        alerts.append("Records nettoyés disponibles — lancez le matching sémantique (Collecte AWA → 🧠 Matcher).")
    elif splits["train"] == 0:
        alerts.append("Paires formées — lancez le nettoyage final (clean, Étape 2 dans Pipeline nettoyage).")

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
        pair_strategy = a.get("pair_strategy", "doc_to_code")
        total_target += target
        total_collected += collected

        # Adapters with pair_strategy=fuse produce pairs via pair_fuser, not from raw records.
        # Their clean_raw count reflects ingredients, not final pairs — avoid misleading missing %.
        if pair_strategy == "fuse":
            # fuse adapters: display raw records collected (progress indicator).
            # Once pair_fuser has run, fused_pairs takes over as the meaningful metric.
            pairs_dir = Path(f"datalake/projects/{project_id}/processed/pairs")
            fused_file = pairs_dir / f"{name}.jsonl"
            fused_count = sum(1 for _ in fused_file.open() if _.strip()) if fused_file.exists() else 0

            # Count clean_raw records and their language split for this adapter
            clean_raw_file = Path(f"datalake/projects/{project_id}/processed/clean_raw/{name}.jsonl")
            clean_raw_java = 0
            clean_raw_python = 0
            if clean_raw_file.exists():
                import json as _json  # noqa: PLC0415
                for line in clean_raw_file.open():
                    if not line.strip(): continue
                    try:
                        r = _json.loads(line)
                        lang = r.get("lang", "").lower()
                        if lang == "java": clean_raw_java += 1
                        elif lang == "python": clean_raw_python += 1
                    except Exception: pass

            # Display fused pairs if available, otherwise raw collected count
            display = fused_count if fused_count > 0 else collected
            missing = max(0, target - display)
            pct = round(display / target * 100) if target > 0 else 0
            status = "ok" if fused_count >= target else ("awaiting_fusion" if fused_count == 0 else "in_progress")
            coverage.append({
                "adapter_name":     name,
                "role":             a.get("role", "specialist"),
                "pair_strategy":    "fuse",
                "lang_pair":        a.get("lang_pair"),
                "target":           target,
                "collected":        display,
                "fused_pairs":      fused_count,
                "clean_raw_java":   clean_raw_java,
                "clean_raw_python": clean_raw_python,
                "missing":          missing,
                "pct":              pct,
                "status":           status,
                "source_configs":   a.get("source_configs", []),
            })
        else:
            missing = max(0, target - collected)
            pct = round(collected / target * 100) if target > 0 else 0
            coverage.append({
                "adapter_name":   name,
                "role":           a.get("role", "specialist"),
                "pair_strategy":  pair_strategy,
                "lang_pair":      None,
                "target":         target,
                "collected":      collected,
                "missing":        missing,
                "pct":            pct,
                "status":         "ok" if missing == 0 else "needs_data",
                "source_configs": a.get("source_configs", []),
            })

    # Separate totals: raw collected vs target pairs
    # For fuse adapters still awaiting fusion, collected = raw records (not yet pairs)
    fuse_pending = any(c["pair_strategy"] == "fuse" and c.get("fused_pairs", 0) == 0 for c in coverage)
    return {
        "project_id":      project_id,
        "total_target":    total_target,
        "total_collected": sum(c["collected"] for c in coverage),
        "total_raw":       len(records),
        "untagged":        untagged,
        "phase":           "collecting" if fuse_pending else "ready",
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


@router.get("/collect-history/{project_id}")
async def get_collect_history(project_id: str) -> dict[str, Any]:
    """Return per-source collection status from data_lineage — replaces localStorage."""
    _ensure_lineage_table()
    rows = fetchall(
        """SELECT adapter_name, source_url, MAX(collected_at) as ts, COUNT(*) as n
           FROM data_lineage
           WHERE project_id = ? AND source_url IS NOT NULL AND source_url != ''
           GROUP BY adapter_name, source_url""",
        (project_id,),
    )
    history: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = f"{project_id}__{row['adapter_name'] or ''}__{row['source_url']}"
        history[key] = {"status": "completed", "ts": row["ts"], "pairs": row["n"]}
    return history


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
