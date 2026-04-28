"""Production feedback — collect thumbs up/down + corrections from model outputs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

_DB_PATH = Path("experiments/mlops.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS production_feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project       TEXT NOT NULL,
                model_id      TEXT NOT NULL,
                adapter_name  TEXT,
                prompt        TEXT NOT NULL,
                output        TEXT NOT NULL,
                correction    TEXT,
                rating        INTEGER NOT NULL CHECK(rating IN (-1, 1)),
                source        TEXT DEFAULT 'ui',
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_project ON production_feedback(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fb_model   ON production_feedback(model_id)")
        conn.commit()


_ensure_table()


class FeedbackSubmit(BaseModel):
    project: str
    model_id: str
    adapter_name: str | None = None
    prompt: str
    output: str
    correction: str | None = None  # human-corrected output when rating == -1
    rating: int                    # +1 (good) | -1 (bad)
    source: str = "ui"


class FeedbackStats(BaseModel):
    project: str
    model_id: str
    total: int
    positive: int
    negative: int
    correction_rate: float
    approval_rate: float


@router.post("/submit", status_code=201)
def submit_feedback(body: FeedbackSubmit) -> dict[str, Any]:
    if body.rating not in (-1, 1):
        raise HTTPException(422, "rating must be -1 or 1")
    if body.rating == -1 and not body.correction:
        raise HTTPException(422, "correction is required when rating is -1")

    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO production_feedback
              (project, model_id, adapter_name, prompt, output, correction, rating, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                body.project, body.model_id, body.adapter_name,
                body.prompt, body.output, body.correction,
                body.rating, body.source, now,
            ),
        )
        conn.commit()
        feedback_id = cur.lastrowid

    return {"id": feedback_id, "created_at": now}


@router.get("/stats/{project}")
def feedback_stats(project: str, model_id: str | None = Query(default=None)) -> list[FeedbackStats]:
    query = "SELECT model_id, COUNT(*) as total, SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END) as pos FROM production_feedback WHERE project=?"
    params: list[Any] = [project]
    if model_id:
        query += " AND model_id=?"
        params.append(model_id)
    query += " GROUP BY model_id"

    corr_query = "SELECT model_id, COUNT(*) as n FROM production_feedback WHERE project=? AND correction IS NOT NULL"
    corr_params: list[Any] = [project]
    if model_id:
        corr_query += " AND model_id=?"
        corr_params.append(model_id)
    corr_query += " GROUP BY model_id"

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        corr_rows = conn.execute(corr_query, corr_params).fetchall()

    corrections_by_model = {r["model_id"]: r["n"] for r in corr_rows}

    results = []
    for row in rows:
        total = row["total"]
        pos = row["pos"]
        neg = total - pos
        corrections = corrections_by_model.get(row["model_id"], 0)
        results.append(FeedbackStats(
            project=project,
            model_id=row["model_id"],
            total=total,
            positive=pos,
            negative=neg,
            correction_rate=round(corrections / total, 4) if total else 0.0,
            approval_rate=round(pos / total, 4) if total else 0.0,
        ))
    return results


@router.get("/corrections/{project}")
def list_corrections(
    project: str,
    model_id: str | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
) -> list[dict[str, Any]]:
    """Return pairs where human provided a correction — used by harvest_feedback.py."""
    query = """
        SELECT id, model_id, adapter_name, prompt, output, correction, created_at
        FROM production_feedback
        WHERE project=? AND correction IS NOT NULL
    """
    params: list[Any] = [project]
    if model_id:
        query += " AND model_id=?"
        params.append(model_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    with _get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]
