"""Multi-adapter orchestrator — intelligent routing + parallel specialist calls + synthesis."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/orchestrate", tags=["orchestrator"])

_DEFAULT_TIMEOUT = 60.0


class OrchestrateRequest(BaseModel):
    project: str
    question: str
    max_specialists: int = 3  # cap on parallel adapter calls
    stream: bool = False


class AdapterInstance(BaseModel):
    name: str
    port: int
    role: str  # 'specialist' | 'orchestrator'


def _load_adapter_registry(project: str) -> list[AdapterInstance]:
    """Read running adapter instances from project config."""
    path = Path("config/projects") / f"{project}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")
    cfg = yaml.safe_load(path.read_text())
    adapters = cfg.get("adapters", [])
    instances = []
    for a in adapters:
        port = a.get("port")
        if port and a.get("status") == "serving":
            instances.append(AdapterInstance(name=a["name"], port=port, role=a.get("role", "specialist")))
    return instances


def _load_orchestrator(instances: list[AdapterInstance]) -> AdapterInstance | None:
    for inst in instances:
        if inst.role == "orchestrator":
            return inst
    return None


def _load_specialists(instances: list[AdapterInstance]) -> list[AdapterInstance]:
    return [i for i in instances if i.role == "specialist"]


async def _call_adapter(client: httpx.AsyncClient, instance: AdapterInstance, prompt: str) -> dict[str, Any]:
    """Call one adapter instance and return its response with timing."""
    t0 = time.perf_counter()
    try:
        resp = await client.post(
            f"http://localhost:{instance.port}/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
                "temperature": 0.1,
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return {
            "adapter": instance.name,
            "response": content,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "adapter": instance.name,
            "response": None,
            "latency_ms": round((time.perf_counter() - t0) * 1000),
            "error": str(exc),
        }


async def _route_via_orchestrator(
    client: httpx.AsyncClient,
    orchestrator: AdapterInstance,
    question: str,
    specialists: list[AdapterInstance],
) -> list[str]:
    """Ask the orchestrator adapter which specialists to call."""
    specialist_list = ", ".join(f"{s.name}" for s in specialists)
    routing_prompt = (
        f"Given the following question, select the most relevant specialists to consult "
        f"from this list: [{specialist_list}].\n\n"
        f"Question: {question}\n\n"
        f"Respond with a JSON array of specialist names only, e.g. [\"droit-societes\", \"droit-ip\"]. "
        f"Select 1 to 3 specialists maximum."
    )
    result = await _call_adapter(client, orchestrator, routing_prompt)
    if result["error"] or not result["response"]:
        # Fallback: call all specialists
        return [s.name for s in specialists]

    import json  # noqa: PLC0415
    raw = result["response"].strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        selected = json.loads(raw)
        if isinstance(selected, list):
            return [s for s in selected if isinstance(s, str)]
    except (json.JSONDecodeError, ValueError):
        pass
    return [s.name for s in specialists]


async def _synthesize_via_orchestrator(
    client: httpx.AsyncClient,
    orchestrator: AdapterInstance,
    question: str,
    specialist_responses: list[dict[str, Any]],
) -> str:
    """Ask the orchestrator to synthesize all specialist responses into one answer."""
    valid = [r for r in specialist_responses if r["response"]]
    if not valid:
        return "Aucun spécialiste n'a pu répondre."

    if len(valid) == 1:
        return valid[0]["response"]

    context_parts = []
    for r in valid:
        context_parts.append(f"### {r['adapter']}\n{r['response']}")
    context = "\n\n".join(context_parts)

    synthesis_prompt = (
        f"You received answers from {len(valid)} specialist advisors for the following question.\n\n"
        f"Question: {question}\n\n"
        f"Specialist responses:\n{context}\n\n"
        f"Synthesize a single coherent, well-structured response that integrates all relevant "
        f"information. Cite which specialist provided which element when relevant."
    )
    result = await _call_adapter(client, orchestrator, synthesis_prompt)
    return result["response"] or context_parts[0]


@router.post("/query")
async def orchestrate_query(body: OrchestrateRequest) -> dict[str, Any]:
    try:
        instances = _load_adapter_registry(body.project)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))

    if not instances:
        raise HTTPException(409, f"No serving adapters found for project '{body.project}'. "
                                  "Start adapter instances first.")

    orchestrator = _load_orchestrator(instances)
    specialists = _load_specialists(instances)

    if not specialists:
        raise HTTPException(409, "No specialist adapters are currently serving.")

    t_start = time.perf_counter()

    async with httpx.AsyncClient() as client:
        # Step 1 — routing
        if orchestrator:
            selected_names = await _route_via_orchestrator(
                client, orchestrator, body.question, specialists
            )
            selected_specialists = [s for s in specialists if s.name in selected_names]
            selected_specialists = selected_specialists[:body.max_specialists]
        else:
            # No orchestrator: call all specialists in parallel
            selected_specialists = specialists[:body.max_specialists]
            selected_names = [s.name for s in selected_specialists]

        # Step 2 — parallel specialist calls
        tasks = [_call_adapter(client, s, body.question) for s in selected_specialists]
        specialist_responses = await asyncio.gather(*tasks)

        # Step 3 — synthesis
        if orchestrator:
            final_answer = await _synthesize_via_orchestrator(
                client, orchestrator, body.question, list(specialist_responses)
            )
        else:
            # No orchestrator: concatenate responses
            parts = [f"[{r['adapter']}]: {r['response']}" for r in specialist_responses if r["response"]]
            final_answer = "\n\n".join(parts)

    total_ms = round((time.perf_counter() - t_start) * 1000)

    return {
        "project": body.project,
        "question": body.question,
        "routing": selected_names,
        "specialist_responses": list(specialist_responses),
        "answer": final_answer,
        "orchestrator_used": orchestrator.name if orchestrator else None,
        "total_latency_ms": total_ms,
    }


@router.get("/status/{project}")
def orchestrator_status(project: str) -> dict[str, Any]:
    """List all adapter instances and their serving status for a project."""
    try:
        instances = _load_adapter_registry(project)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))

    return {
        "project": project,
        "instances": [i.model_dump() for i in instances],
        "n_specialists": sum(1 for i in instances if i.role == "specialist"),
        "orchestrator": next((i.name for i in instances if i.role == "orchestrator"), None),
        "ready": len(instances) > 0,
    }
