"""Corrective memory store — SQLite + Kuzu KG + Qdrant.

SQLite is the source of truth for all corrections (always available).
Kuzu mirrors structured facts (hardware→tps, correction nodes).
Qdrant indexes validated corrections for semantic few-shot retrieval.

All three are optional — SQLite always runs, KG/Qdrant are best-effort.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

_DB_PATH = Path("memory/corrections.db")

_HARDWARE_PROFILES = Path("memory/benchmarks/hardware_profiles.json")

# KG + vector stores — imported lazily to avoid hard dependency
def _kg():
    from pipeline.utils import kg_store
    return kg_store

def _vec():
    from pipeline.utils import vector_corrections
    return vector_corrections


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
        CREATE INDEX IF NOT EXISTS idx_corrections_phase   ON corrections(phase);
        CREATE INDEX IF NOT EXISTS idx_corrections_status  ON corrections(status);
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
    # Mirror to KG (staging)
    try:
        correction = get_correction(correction_id)
        if correction:
            _kg().add_correction_to_kg(correction, env="staging")
    except Exception as e:
        logger.debug("KG staging mirror failed (non-critical): %s", e)
    return correction_id


def validate_correction(correction_id: str, validated_by: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE corrections SET status='validated', validated_at=?, validated_by=?
               WHERE id=? AND status='staging'""",
            (now, validated_by, correction_id),
        )
        updated = cursor.rowcount > 0
    if not updated:
        return False
    correction = get_correction(correction_id)
    if correction:
        # Promote to KG production
        try:
            _kg().add_correction_to_kg(correction, env="production")
            _kg().validate_correction_in_kg(correction_id, validated_by, now, env="production")
        except Exception as e:
            logger.debug("KG production mirror failed (non-critical): %s", e)
        # Index in Qdrant for semantic retrieval
        try:
            _vec().upsert_correction(correction)
        except Exception as e:
            logger.debug("Qdrant index failed (non-critical): %s", e)
    return True


def reject_correction(correction_id: str, reason: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            """UPDATE corrections SET status='rejected', rejected_at=?, rejection_reason=?
               WHERE id=? AND status='staging'""",
            (now, reason, correction_id),
        )
        updated = cursor.rowcount > 0
    if updated:
        try:
            _vec().delete_correction(correction_id)
        except Exception:
            pass
    return updated


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
            "SELECT phase, status, COUNT(*) as count FROM corrections GROUP BY phase, status"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) as c FROM corrections").fetchone()["c"]
    stats: dict[str, dict] = {}
    for row in rows:
        phase = row["phase"]
        if phase not in stats:
            stats[phase] = {"staging": 0, "validated": 0, "rejected": 0}
        stats[phase][row["status"]] = row["count"]
    return {
        "by_phase": stats,
        "total": total,
        "kg_available": _kg().is_available(),
        "vector_available": _vec().is_available(),
    }


def build_memory_context(phase: str, need: str = "", hw_description: str = "") -> str:
    """Build a structured text block to inject into advisor prompts.

    Sources (in order of priority):
    1. SQLite validated corrections — always available
    2. KG hardware TPS facts — if kuzu available
    3. Qdrant semantic few-shot — if Qdrant + Ollama available
    """
    lines: list[str] = []

    # 1 — Validated corrections from SQLite (deterministic)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM corrections WHERE status='validated' AND phase=? ORDER BY confidence DESC LIMIT 10",
            (phase,),
        ).fetchall()
    if rows:
        lines.append("[CORRECTIONS VALIDÉES]")
        for row in rows:
            line = f"- {row['description']}"
            if row["expected_value"]:
                line += f" → valeur correcte : {row['expected_value']}"
            if row["rationale"]:
                line += f" ({row['rationale']})"
            lines.append(line)
        lines.append("[FIN CORRECTIONS]")

    # 2 — KG hardware TPS facts (if hw_description provided + kuzu available)
    if hw_description and _kg().is_available():
        try:
            kg = _kg()
            # Try to match hw_description against known hardware names
            known_hw = ["Apple M4 Pro", "Apple M3 Ultra", "NVIDIA A100 80GB",
                        "NVIDIA H100 80GB SXM", "NVIDIA RTX 4090 24GB", "CPU-only x86"]
            matched = next(
                (h for h in known_hw if any(kw in hw_description.lower()
                 for kw in h.lower().split()[-2:])),
                None,
            )
            if matched:
                tps_facts = kg.get_hardware_tps(matched, env="production")
                if tps_facts:
                    lines.append(f"[FAITS MESURÉS — {matched}]")
                    for f in tps_facts:
                        lines.append(f"- {f['model']} : {f['tps']} t/s, TTFT {f['ttft_ms']} ms (source: {f['source']})")
                    lines.append("[FIN FAITS MESURÉS]")
        except Exception as e:
            logger.debug("KG hw facts failed: %s", e)

    # 3 — Semantic few-shot from Qdrant (if available)
    if need and _vec().is_available():
        try:
            hits = _vec().search_similar_corrections(query=need, phase=phase, top_k=3)
            new_hits = [h for h in hits if h.get("description") not in {r["description"] for r in rows}]
            if new_hits:
                lines.append("[EXEMPLES SIMILAIRES VALIDÉS]")
                for h in new_hits:
                    line = f"- {h['description']}"
                    if h.get("expected_value"):
                        line += f" → {h['expected_value']}"
                    lines.append(line)
                lines.append("[FIN EXEMPLES]")
        except Exception as e:
            logger.debug("Qdrant few-shot failed: %s", e)

    return "\n".join(lines)


def seed_kg_from_profiles(env: str = "production") -> int:
    """Load hardware_profiles.json into KG. Call once after benchmark."""
    try:
        return _kg().load_hardware_profiles_to_kg(_HARDWARE_PROFILES, env=env)
    except Exception as e:
        logger.warning("KG seeding failed: %s", e)
        return 0
