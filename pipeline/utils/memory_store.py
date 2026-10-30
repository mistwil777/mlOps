"""Corrective memory store — SQLite backend (Kuzu migration in Étape 3).

Stages corrections through staging before production.
Never serves staging corrections as authoritative facts.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

_DB_PATH = Path("memory/corrections.db")
_SCHEMA_VERSION = 1


def _connect(path: Path = _DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS corrections (
            id              TEXT PRIMARY KEY,
            phase           TEXT NOT NULL,
            description     TEXT NOT NULL,
            observed_value  TEXT,
            expected_value  TEXT,
            rationale       TEXT,
            confidence      REAL DEFAULT 0.5,
            source          TEXT NOT NULL DEFAULT 'human',
            status          TEXT NOT NULL DEFAULT 'staging',
            context_need    TEXT,
            context_hw      TEXT,
            reported_by     TEXT,
            created_at      TEXT NOT NULL,
            validated_at    TEXT,
            validated_by    TEXT,
            rejected_at     TEXT,
            rejection_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_corrections_phase  ON corrections(phase);
        CREATE INDEX IF NOT EXISTS idx_corrections_status ON corrections(status);
        CREATE INDEX IF NOT EXISTS idx_corrections_created ON corrections(created_at);
    """)
    conn.commit()


def submit_correction(
    phase: str,
    description: str,
    observed_value: str | None = None,
    expected_value: str | None = None,
    rationale: str | None = None,
    context_need: str | None = None,
    context_hw: str | None = None,
    reported_by: str | None = None,
    confidence: float = 0.5,
    source: Literal["human", "llm_consensus", "measured"] = "human",
) -> str:
    correction_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO corrections
               (id, phase, description, observed_value, expected_value, rationale,
                confidence, source, status, context_need, context_hw, reported_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,'staging',?,?,?,?)""",
            (correction_id, phase, description, observed_value, expected_value,
             rationale, confidence, source, context_need, context_hw, reported_by, now),
        )
    return correction_id


def validate_correction(correction_id: str, validated_by: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE corrections SET status='validated', validated_at=?, validated_by=?
               WHERE id=? AND status='staging'""",
            (now, validated_by, correction_id),
        )
        return cursor.rowcount > 0


def reject_correction(correction_id: str, reason: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE corrections SET status='rejected', rejected_at=?, rejection_reason=?
               WHERE id=? AND status='staging'""",
            (now, reason, correction_id),
        )
        return cursor.rowcount > 0


def list_corrections(
    status: Literal["staging", "validated", "rejected"] | None = None,
    phase: str | None = None,
    limit: int = 50,
) -> list[dict]:
    query = "SELECT * FROM corrections WHERE 1=1"
    params: list = []
    if status:
        query += " AND status=?"
        params.append(status)
    if phase:
        query += " AND phase=?"
        params.append(phase)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_correction(correction_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM corrections WHERE id=?", (correction_id,)
        ).fetchone()
    return dict(row) if row else None


def get_stats() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT phase, status, COUNT(*) as count
               FROM corrections GROUP BY phase, status"""
        ).fetchall()
    stats: dict[str, dict] = {}
    for row in rows:
        phase = row["phase"]
        if phase not in stats:
            stats[phase] = {"staging": 0, "validated": 0, "rejected": 0}
        stats[phase][row["status"]] = row["count"]
    total = conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] if False else None
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) as c FROM corrections").fetchone()["c"]
    return {"by_phase": stats, "total": total}


def build_memory_context(phase: str, need: str = "", hw_description: str = "") -> str:
    """Build a text block of validated corrections to inject into prompts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM corrections WHERE status='validated' AND phase=? ORDER BY confidence DESC LIMIT 10",
            (phase,),
        ).fetchall()
    if not rows:
        return ""
    lines = ["[CORRECTIONS VALIDÉES]"]
    for row in rows:
        line = f"- {row['description']}"
        if row["expected_value"]:
            line += f" → valeur correcte : {row['expected_value']}"
        if row["rationale"]:
            line += f" ({row['rationale']})"
        lines.append(line)
    lines.append("[FIN CORRECTIONS]")
    return "\n".join(lines)
