"""Memory router — corrective memory store endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pipeline.utils.memory_store import (
    build_memory_context,
    get_correction,
    get_stats,
    list_corrections,
    reject_correction,
    submit_correction,
    validate_correction,
)

router = APIRouter(prefix="/api/memory", tags=["memory"])


class CorrectionPayload(BaseModel):
    phase: str
    description: str
    observed_value: str | None = None
    expected_value: str | None = None
    rationale: str | None = None
    context_need: str | None = None
    context_hw: str | None = None
    reported_by: str | None = None
    confidence: float = 0.5
    source: str = "human"


class ValidatePayload(BaseModel):
    validated_by: str = "user"


class RejectPayload(BaseModel):
    reason: str


@router.post("/correction", status_code=201)
def post_correction(payload: CorrectionPayload) -> dict:
    correction_id = submit_correction(
        phase=payload.phase,
        description=payload.description,
        observed_value=payload.observed_value,
        expected_value=payload.expected_value,
        rationale=payload.rationale,
        context_need=payload.context_need,
        context_hw=payload.context_hw,
        reported_by=payload.reported_by,
        confidence=payload.confidence,
        source=payload.source,  # type: ignore[arg-type]
    )
    return {"id": correction_id, "status": "staging"}


@router.get("/corrections")
def get_corrections(
    status: str | None = None,
    phase: str | None = None,
    limit: int = 50,
) -> list[dict]:
    return list_corrections(status=status, phase=phase, limit=limit)  # type: ignore[arg-type]


@router.get("/corrections/{correction_id}")
def get_correction_by_id(correction_id: str) -> dict:
    c = get_correction(correction_id)
    if not c:
        raise HTTPException(404, "Correction not found")
    return c


@router.post("/validate/{correction_id}")
def post_validate(correction_id: str, payload: ValidatePayload) -> dict:
    ok = validate_correction(correction_id, payload.validated_by)
    if not ok:
        raise HTTPException(404, "Correction not found or not in staging")
    return {"id": correction_id, "status": "validated"}


@router.post("/reject/{correction_id}")
def post_reject(correction_id: str, payload: RejectPayload) -> dict:
    ok = reject_correction(correction_id, payload.reason)
    if not ok:
        raise HTTPException(404, "Correction not found or not in staging")
    return {"id": correction_id, "status": "rejected"}


@router.get("/stats")
def get_memory_stats() -> dict:
    return get_stats()


@router.get("/context")
def get_memory_context(phase: str, need: str = "", hw_description: str = "") -> dict:
    ctx = build_memory_context(phase=phase, need=need, hw_description=hw_description)
    return {"context": ctx, "has_corrections": bool(ctx)}
