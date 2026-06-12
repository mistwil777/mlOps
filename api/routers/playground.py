"""Playground: model test, comparison, batch evaluation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
from fastapi import APIRouter, HTTPException
from pipeline.utils.llm import call_llm as _call_llm_backend
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

router = APIRouter(prefix="/api/playground", tags=["playground"])

_MLX_SERVER_URL = "http://localhost:8080"


class InferRequest(BaseModel):
    prompt: str
    model: str = "local"                  # "local" | "nova-pro"
    max_tokens: int = 1024
    temperature: float = 0.1
    rag_project: str | None = None        # inject RAG context from this project if set


class BatchRequest(BaseModel):
    prompts: list[str]
    model: str = "local"
    reference_completions: list[str] | None = None
    max_tokens: int = 512
    temperature: float = 0.1


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


@router.post("/infer")
async def infer(req: InferRequest) -> dict[str, Any]:
    """Single inference, returns completion + latency."""
    rag_ctx = ""
    if req.rag_project:
        rag_ctx = _get_rag_context(req.rag_project, req.prompt)

    full_prompt = req.prompt
    if rag_ctx:
        full_prompt = f"[Contexte RAG]\n{rag_ctx}\n\n{req.prompt}"

    t0 = time.perf_counter()
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


@router.post("/compare")
async def compare(req: BatchRequest) -> dict[str, Any]:
    """Compare local model vs Nova Pro on the same prompts."""
    if not req.prompts:
        raise HTTPException(400, "No prompts provided")

    from pipeline.utils.metrics import rouge_l_score  # noqa: PLC0415

    local_ok = _mlx_available()
    results = []

    for i, prompt in enumerate(req.prompts):
        row: dict[str, Any] = {"prompt_preview": prompt[:100]}

        # Local
        if local_ok:
            comp, lat = _call_mlx(prompt, req.max_tokens, req.temperature)
            row["local"] = {"completion": comp, "latency_ms": round(lat, 1)}
        else:
            row["local"] = {"completion": None, "latency_ms": None, "error": "server unavailable"}

        # Nova Pro
        try:
            comp_n, lat_n = _call_nova(prompt, req.max_tokens, req.temperature)
            row["nova_pro"] = {"completion": comp_n, "latency_ms": round(lat_n, 1)}
        except Exception as exc:
            row["nova_pro"] = {"completion": None, "latency_ms": None, "error": str(exc)}

        # ROUGE-L if reference available
        if req.reference_completions and i < len(req.reference_completions):
            ref = req.reference_completions[i]
            if row["local"].get("completion"):
                row["local"]["rouge_l"] = rouge_l_score(row["local"]["completion"], ref)
            if row["nova_pro"].get("completion"):
                row["nova_pro"]["rouge_l"] = rouge_l_score(row["nova_pro"]["completion"], ref)

        results.append(row)

    return {"results": results, "n": len(results)}


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
