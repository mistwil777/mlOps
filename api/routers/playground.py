"""Playground: model test, comparison, batch evaluation."""

from __future__ import annotations

import json
import math
import random
import time
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
import yaml
from fastapi import APIRouter, HTTPException, Query
from pipeline.utils.llm import call_llm as _call_llm_backend
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/playground", tags=["playground"])

_MLX_SERVER_URL = "http://localhost:8080"
_MCNEMAR_MIN_DISCORDANT = 10   # minimum b+c for a meaningful McNemar test


class InferRequest(BaseModel):
    prompt: str
    model: str = "local"            # "local" | "nova-pro"
    max_tokens: int = 1024
    temperature: float = 0.1
    rag_project: str | None = None


class BatchRequest(BaseModel):
    prompts: list[str]
    project_id: str | None = None   # used to load domain from config
    reference_completions: list[str | None] | None = None
    max_tokens: int = 512
    temperature: float = 0.1


class SuggestReferenceRequest(BaseModel):
    prompt: str
    project_id: str
    top_k: int = 3


class SaveTestSetRequest(BaseModel):
    project_id: str
    name: str                           # e.g. "migrator-baseline"
    prompts: list[str]
    reference_completions: list[str | None]
    description: str | None = None


def _mlx_available() -> bool:
    try:
        r = httpx.get(f"{_MLX_SERVER_URL}/v1/models", timeout=2.0)
        return r.status_code == 200 and len(r.json().get("data", [])) > 0
    except Exception:
        return False


def _get_rag_context(project_id: str, query: str, k: int = 3) -> str:
    try:
        from pipeline.rag_index import RagIndex  # noqa: PLC0415
        idx = RagIndex(project_id)
        results = idx.search(query[:512], k=k)
        return idx.format_context(results)
    except Exception:
        return ""


def _load_project_domain(project_id: str) -> str:
    try:
        cfg_path = Path(f"config/projects/{project_id}.yaml")
        with cfg_path.open() as fh:
            return yaml.safe_load(fh).get("domain", "text")
    except Exception:
        return "text"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Inference endpoints ────────────────────────────────────────────────────────

@router.post("/infer")
async def infer(req: InferRequest) -> dict[str, Any]:
    """Single inference, returns completion + latency."""
    rag_ctx = ""
    if req.rag_project:
        rag_ctx = _get_rag_context(req.rag_project, req.prompt)

    full_prompt = req.prompt
    if rag_ctx:
        full_prompt = f"[Contexte RAG]\n{rag_ctx}\n\n{req.prompt}"

    if req.model == "nova-pro":
        completion, latency = _call_nova(full_prompt, req.max_tokens, req.temperature)
    else:
        if not _mlx_available():
            raise HTTPException(503, "mlx_lm.server is not running or has no model loaded")
        completion, latency = _call_mlx(full_prompt, req.max_tokens, req.temperature)

    return {
        "completion": completion,
        "latency_ms": round(latency, 1),
        "model": req.model,
        "rag_context": rag_ctx,
        "prompt_len": len(req.prompt.split()),
        "completion_len": len(completion.split()),
    }


@router.post("/infer/stream")
async def infer_stream(req: InferRequest):
    """Single inference with SSE token streaming (mlx_lm.server only)."""
    if req.model != "local":
        raise HTTPException(400, "Streaming is only supported for the local model")
    if not _mlx_available():
        raise HTTPException(503, "mlx_lm.server is not running or has no model loaded")

    rag_ctx = ""
    if req.rag_project:
        rag_ctx = _get_rag_context(req.rag_project, req.prompt)

    full_prompt = req.prompt
    if rag_ctx:
        full_prompt = f"[Contexte RAG]\n{rag_ctx}\n\n{req.prompt}"

    async def generator() -> AsyncGenerator[dict, None]:
        yield {"event": "rag_context", "data": json.dumps({"context": rag_ctx})}

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{_MLX_SERVER_URL}/v1/chat/completions",
                json={
                    "model": "default",
                    "messages": [{"role": "user", "content": full_prompt}],
                    "max_tokens": req.max_tokens,
                    "temperature": req.temperature,
                    "stream": True,
                },
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload == "[DONE]":
                            yield {"event": "done", "data": "{}"}
                            return
                        try:
                            chunk = json.loads(payload)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield {"event": "token", "data": json.dumps({"token": delta})}
                        except Exception:
                            pass

    return EventSourceResponse(generator())


# ── Suggest reference / prompt ─────────────────────────────────────────────────

@router.post("/suggest-reference")
async def suggest_reference(req: SuggestReferenceRequest) -> dict[str, Any]:
    """Return top-k training examples closest to the query prompt by embedding similarity.

    When prompt is empty, returns a random sample (useful for browsing cold-start).
    """
    from pipeline.utils.jsonl import read_jsonl  # noqa: PLC0415
    from pipeline.utils.embeddings import embed as _embed  # noqa: PLC0415

    candidates: list[dict[str, Any]] = []
    for suffix in ("fine-tuning-ready", "processed"):
        base = Path(f"datalake/projects/{req.project_id}") / suffix
        if base.exists():
            for f in base.rglob("*.jsonl"):
                for r in read_jsonl(f):
                    if r.get("prompt") and r.get("completion"):
                        candidates.append(r)

    if not candidates:
        return {"suggestions": [], "warning": f"Aucun exemple trouvé pour le projet '{req.project_id}'."}

    # Empty prompt → random sample (cold-start browsing)
    if not req.prompt.strip():
        sample = random.sample(candidates, min(req.top_k, len(candidates)))
        return {
            "suggestions": [_fmt_candidate(c, 0.0) for c in sample],
            "total_candidates": len(candidates),
            "mode": "random",
        }

    texts = [req.prompt[:300]] + [c["prompt"][:300] for c in candidates]
    vecs = _embed(texts)

    if not vecs or len(vecs) < 2:
        return {"suggestions": [], "warning": "Embedding indisponible — suggestions impossibles."}

    query_vec = vecs[0]
    # Compute similarity once per candidate, sort, slice
    scored = sorted(
        ((c, _cosine(query_vec, v)) for c, v in zip(candidates, vecs[1:])),
        key=lambda pair: pair[1],
        reverse=True,
    )

    return {
        "suggestions": [_fmt_candidate(c, sim) for c, sim in scored[: req.top_k]],
        "total_candidates": len(candidates),
        "mode": "similarity",
    }


def _fmt_candidate(c: dict[str, Any], similarity: float) -> dict[str, Any]:
    return {
        "prompt":            c["prompt"],
        "prompt_preview":    c["prompt"][:120],
        "completion":        c["completion"],
        "completion_preview": c["completion"][:120],
        "similarity":        round(similarity, 3),
        "adapter_name":      c.get("adapter_name"),
        "source_url":        c.get("source_url"),
    }


# ── Compare (McNemar) ──────────────────────────────────────────────────────────

@router.post("/compare")
async def compare(req: BatchRequest) -> dict[str, Any]:
    """Compare local model vs Nova Pro.

    Correctness criterion (per prompt):
    - If reference provided AND project domain = "code" → evaluators.evaluate() (exec/AST)
    - If reference provided AND domain = "text" → ROUGE-L ≥ 0.4
    - No reference → heuristic (non-empty, no error patterns)

    Returns Wilson 95% CI + McNemar test + discordant pair count with reliability flag.
    """
    if not req.prompts:
        raise HTTPException(400, "No prompts provided")

    from pipeline.utils.evaluators import evaluate as _evaluate  # noqa: PLC0415
    from pipeline.utils.stats import wilson_ci, mcnemar_test, sample_size_warning  # noqa: PLC0415

    domain = _load_project_domain(req.project_id) if req.project_id else "text"
    local_ok = _mlx_available()
    results: list[dict[str, Any]] = []
    local_valid: list[bool] = []
    nova_valid: list[bool] = []

    refs = req.reference_completions or []
    has_references = any(r for r in refs if r)

    for i, prompt in enumerate(req.prompts):
        row: dict[str, Any] = {"prompt_preview": prompt[:100]}
        ref = (refs[i] if i < len(refs) else None) or None

        # Local model
        if local_ok:
            comp, lat = _call_mlx(prompt, req.max_tokens, req.temperature)
            ev = _evaluate(comp, ref, domain)
            row["local"] = {
                "completion": comp,
                "latency_ms": round(lat, 1),
                "valid": ev.correct,
                "evaluator_method": ev.method,
                "evaluator_score": round(ev.score, 3),
                "evaluator_detail": ev.detail,
            }
            local_valid.append(ev.correct)
        else:
            row["local"] = {"completion": None, "latency_ms": None, "error": "server unavailable", "valid": False, "evaluator_method": "none"}
            local_valid.append(False)

        # Nova Pro
        try:
            comp_n, lat_n = _call_nova(prompt, req.max_tokens, req.temperature)
            ev_n = _evaluate(comp_n, ref, domain)
            row["nova_pro"] = {
                "completion": comp_n,
                "latency_ms": round(lat_n, 1),
                "valid": ev_n.correct,
                "evaluator_method": ev_n.method,
                "evaluator_score": round(ev_n.score, 3),
                "evaluator_detail": ev_n.detail,
            }
            nova_valid.append(ev_n.correct)
        except Exception as exc:
            row["nova_pro"] = {"completion": None, "latency_ms": None, "error": str(exc), "valid": False, "evaluator_method": "none"}
            nova_valid.append(False)

        results.append(row)

    n = len(results)
    local_ci = wilson_ci(sum(local_valid), n)
    nova_ci = wilson_ci(sum(nova_valid), n)
    mc = mcnemar_test(local_valid, nova_valid)
    size_warn = sample_size_warning(n)

    discordant = mc.b + mc.c
    reliable = discordant >= _MCNEMAR_MIN_DISCORDANT

    stats = {
        "n": n,
        "domain": domain,
        "evaluator_mode": "reference" if has_references else "heuristic",
        "local": {
            "pass_rate": local_ci.point,
            "ci_low": local_ci.low,
            "ci_high": local_ci.high,
            "ci_margin": local_ci.margin,
        },
        "nova_pro": {
            "pass_rate": nova_ci.point,
            "ci_low": nova_ci.low,
            "ci_high": nova_ci.high,
            "ci_margin": nova_ci.margin,
        },
        "mcnemar": {
            "b": mc.b,
            "c": mc.c,
            "discordant": discordant,
            "reliable": reliable,
            "reliability_warning": (
                None if reliable else
                f"Seulement {discordant} paires discordantes (minimum : {_MCNEMAR_MIN_DISCORDANT}). "
                f"Résultat non significatif quel que soit p."
            ),
            "p_value": mc.p_value,
            "significant": mc.significant,
            "winner": mc.winner,
            "interpretation": _mcnemar_message(mc.winner, mc.significant, mc.p_value, reliable),
        },
        "sample_size": size_warn,
        "references_used": sum(1 for r in refs if r),
    }

    return {"results": results, "n": n, "stats": stats}


def _mcnemar_message(winner: str, significant: bool, p_value: float, reliable: bool) -> str:
    if not reliable:
        return "Pas assez de paires discordantes pour conclure. Ajoutez des prompts."
    if not significant:
        return f"Aucune différence significative (p={p_value:.3f}). Augmentez le nombre d'exemples."
    model_name = "Local (mlx_lm)" if winner == "a" else "Nova Pro"
    return f"{model_name} est statistiquement meilleur (p={p_value:.3f} < 0.05)."


# ── Test sets (named + versioned) ──────────────────────────────────────────────

@router.post("/testsets")
async def save_testset(req: SaveTestSetRequest) -> dict[str, Any]:
    """Save a named test set to datalake/projects/<id>/testsets/<name>/vN.json."""
    import datetime  # noqa: PLC0415

    name_clean = req.name.strip().replace(" ", "_").replace("/", "_")
    if not name_clean:
        raise HTTPException(400, "name cannot be empty")

    ts_dir = Path(f"datalake/projects/{req.project_id}/testsets/{name_clean}")
    ts_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(ts_dir.glob("v*.json"))
    next_v = len(existing) + 1

    path = ts_dir / f"v{next_v}.json"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    payload = {
        "name": name_clean,
        "version": next_v,
        "project_id": req.project_id,
        "saved_at": now,
        "description": req.description,
        "n_prompts": len(req.prompts),
        "prompts": req.prompts,
        "reference_completions": req.reference_completions,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"saved": True, "path": str(path), "name": name_clean, "version": next_v}


@router.get("/testsets/{project_id}")
async def list_testsets(project_id: str) -> list[dict[str, Any]]:
    """List available test sets for a project."""
    base = Path(f"datalake/projects/{project_id}/testsets")
    if not base.exists():
        return []

    result = []
    for ts_dir in sorted(base.iterdir()):
        if not ts_dir.is_dir():
            continue
        versions = []
        for vf in sorted(ts_dir.glob("v*.json")):
            try:
                meta = json.loads(vf.read_text(encoding="utf-8"))
                versions.append({
                    "version": meta.get("version"),
                    "saved_at": meta.get("saved_at"),
                    "n_prompts": meta.get("n_prompts"),
                    "description": meta.get("description"),
                })
            except Exception:
                pass
        if versions:
            result.append({"name": ts_dir.name, "versions": versions})
    return result


@router.get("/testsets/{project_id}/{name}")
async def load_testset(
    project_id: str,
    name: str,
    version: int | None = Query(default=None),
) -> dict[str, Any]:
    """Load the latest (or specified) version of a test set."""
    ts_dir = Path(f"datalake/projects/{project_id}/testsets/{name}")
    if not ts_dir.exists():
        raise HTTPException(404, f"Test set '{name}' not found for project '{project_id}'")

    versions = sorted(ts_dir.glob("v*.json"))
    if not versions:
        raise HTTPException(404, "No versions found")

    if version is not None:
        target = ts_dir / f"v{version}.json"
        if not target.exists():
            raise HTTPException(404, f"Version {version} not found")
    else:
        target = versions[-1]

    return json.loads(target.read_text(encoding="utf-8"))


# ── Private helpers ────────────────────────────────────────────────────────────

def _call_mlx(prompt: str, max_tokens: int, temperature: float) -> tuple[str, float]:
    t0 = time.perf_counter()
    resp = httpx.post(
        f"{_MLX_SERVER_URL}/v1/chat/completions",
        json={
            "model": "default",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=120.0,
    )
    latency = (time.perf_counter() - t0) * 1000
    if resp.status_code != 200:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()["choices"][0]["message"]["content"], latency


def _call_nova(prompt: str, max_tokens: int, temperature: float) -> tuple[str, float]:
    t0 = time.perf_counter()
    result = _call_llm_backend(prompt, max_tokens=max_tokens, temperature=temperature) or ""
    latency = (time.perf_counter() - t0) * 1000
    return result, latency
