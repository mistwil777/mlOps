"""LLM call audit: consult (read) and receive records from Celery workers (write, internal)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.routers.jobs import _check_token
from pipeline.utils.run_ledger import get_ledger

router = APIRouter(prefix="/api/llm-calls", tags=["llm-audit"])


class LLMCallRecord(BaseModel):
    id: str
    ts: float
    project_id: Optional[str] = None
    level: Optional[str] = None
    purpose: Optional[str] = None
    decision: str
    backend: Optional[str] = None
    model: Optional[str] = None
    entities_json: Optional[str] = None
    sent_sha256: Optional[str] = None
    sent_chars: Optional[int] = None
    sent_text: Optional[str] = None


class LLMCallOutcome(BaseModel):
    ok: bool
    latency_ms: Optional[int] = None
    response_chars: Optional[int] = None
    error: Optional[str] = None


@router.get("")
def list_llm_calls(project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """What was sent to which LLM backend, per project, newest first."""
    return get_ledger().list_llm_calls(project_id, min(max(limit, 1), 500))


@router.post("")
def record_llm_call(
    body: LLMCallRecord,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, bool]:
    _check_token(x_internal_token)
    get_ledger().record_llm_call(body.model_dump())
    return {"ok": True}


@router.post("/{call_id}/outcome")
def record_llm_outcome(
    call_id: str,
    body: LLMCallOutcome,
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> dict[str, bool]:
    _check_token(x_internal_token)
    get_ledger().finish_llm_call(
        call_id, ok=body.ok, latency_ms=body.latency_ms, response_chars=body.response_chars, error=body.error
    )
    return {"ok": True}
