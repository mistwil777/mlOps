"""Kuzu knowledge graph store for structured corrective memory.

Schema:
  Hardware -[:VALIDATED_TPS]-> ModelProfile
  Hardware -[:VALIDATED_TTFT]-> ModelProfile
  ArchitecturePattern -[:CORRECT_FOR]-> UseCase
  Correction -[:FIXES]-> AdvisorPhase
  Correction -[:VALIDATED_BY]-> Human

Gracefully unavailable if kuzu is not installed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KG_BASE = Path("memory")

try:
    import kuzu as _kuzu
    _KUZU_AVAILABLE = True
except ImportError:
    _KUZU_AVAILABLE = False
    logger.warning("kuzu not installed — KG store disabled. Run: pip install kuzu>=0.7.0")


def _db_path(env: str) -> Path:
    p = _KG_BASE / env / "kg"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _open(env: str = "production"):
    if not _KUZU_AVAILABLE:
        raise RuntimeError("kuzu not installed")
    db = _kuzu.Database(str(_db_path(env)))
    conn = _kuzu.Connection(db)
    return db, conn


def init_schema(env: str = "production") -> None:
    """Create node/relationship tables if they don't exist."""
    if not _KUZU_AVAILABLE:
        return
    _, conn = _open(env)
    stmts = [
        # Node tables
        "CREATE NODE TABLE IF NOT EXISTS Hardware (name STRING, ram_gb INT64, type STRING, PRIMARY KEY(name))",
        "CREATE NODE TABLE IF NOT EXISTS ModelProfile (name STRING, size_b INT64, quantization STRING, PRIMARY KEY(name))",
        "CREATE NODE TABLE IF NOT EXISTS ArchitecturePattern (pattern_type STRING, domain STRING, PRIMARY KEY(pattern_type))",
        "CREATE NODE TABLE IF NOT EXISTS AdvisorPhase (name STRING, PRIMARY KEY(name))",
        "CREATE NODE TABLE IF NOT EXISTS Human (name STRING, PRIMARY KEY(name))",
        "CREATE NODE TABLE IF NOT EXISTS Correction (id STRING, phase STRING, description STRING, observed_value STRING, expected_value STRING, confidence DOUBLE, status STRING, created_at STRING, PRIMARY KEY(id))",
        # Relationship tables
        "CREATE REL TABLE IF NOT EXISTS VALIDATED_TPS (FROM Hardware TO ModelProfile, tps DOUBLE, ttft_ms DOUBLE, measured_at STRING, source STRING)",
        "CREATE REL TABLE IF NOT EXISTS VALIDATED_TTFT (FROM Hardware TO ModelProfile, ttft_ms DOUBLE, measured_at STRING)",
        "CREATE REL TABLE IF NOT EXISTS CORRECT_FOR (FROM ArchitecturePattern TO AdvisorPhase, domain STRING, sovereignty BOOLEAN, volume_range STRING)",
        "CREATE REL TABLE IF NOT EXISTS FIXES (FROM Correction TO AdvisorPhase)",
        "CREATE REL TABLE IF NOT EXISTS VALIDATED_BY (FROM Correction TO Human, validated_at STRING)",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except Exception as e:
            if "already exists" not in str(e).lower():
                logger.warning("KG schema stmt failed: %s — %s", stmt[:60], e)


def upsert_hardware_tps(
    hw_name: str,
    model_name: str,
    tps: float,
    ttft_ms: float,
    measured_at: str,
    ram_gb: int = 0,
    hw_type: str = "apple_silicon",
    env: str = "production",
) -> None:
    if not _KUZU_AVAILABLE:
        return
    init_schema(env)
    _, conn = _open(env)
    # Upsert Hardware node
    try:
        conn.execute("CREATE (h:Hardware {name: $name, ram_gb: $ram, type: $type})",
                     {"name": hw_name, "ram": ram_gb, "type": hw_type})
    except Exception:
        pass  # already exists
    # Upsert ModelProfile node
    try:
        conn.execute("CREATE (m:ModelProfile {name: $name, size_b: 7, quantization: '4bit'})",
                     {"name": model_name})
    except Exception:
        pass
    # Delete old edge and re-create (Kuzu doesn't have MERGE on rels yet)
    try:
        conn.execute(
            "MATCH (h:Hardware {name: $hw})-[r:VALIDATED_TPS]->(m:ModelProfile {name: $model}) DELETE r",
            {"hw": hw_name, "model": model_name},
        )
    except Exception:
        pass
    conn.execute(
        "MATCH (h:Hardware {name: $hw}), (m:ModelProfile {name: $model}) "
        "CREATE (h)-[:VALIDATED_TPS {tps: $tps, ttft_ms: $ttft, measured_at: $at, source: 'measured'}]->(m)",
        {"hw": hw_name, "model": model_name, "tps": tps, "ttft": ttft_ms, "at": measured_at},
    )


def get_hardware_tps(hw_name: str, env: str = "production") -> list[dict]:
    """Return all validated TPS facts for a given hardware."""
    if not _KUZU_AVAILABLE:
        return []
    init_schema(env)
    _, conn = _open(env)
    try:
        result = conn.execute(
            "MATCH (h:Hardware {name: $hw})-[r:VALIDATED_TPS]->(m:ModelProfile) "
            "RETURN m.name AS model, r.tps AS tps, r.ttft_ms AS ttft_ms, r.measured_at AS measured_at, r.source AS source",
            {"hw": hw_name},
        )
        rows = []
        while result.has_next():
            row = result.get_next()
            rows.append({"model": row[0], "tps": row[1], "ttft_ms": row[2], "measured_at": row[3], "source": row[4]})
        return rows
    except Exception as e:
        logger.warning("KG get_hardware_tps failed: %s", e)
        return []


def add_correction_to_kg(correction: dict, env: str = "production") -> None:
    if not _KUZU_AVAILABLE:
        return
    init_schema(env)
    _, conn = _open(env)
    cid = correction["id"]
    try:
        conn.execute(
            "CREATE (c:Correction {id: $id, phase: $phase, description: $desc, "
            "observed_value: $obs, expected_value: $exp, confidence: $conf, "
            "status: $status, created_at: $created})",
            {
                "id": cid,
                "phase": correction.get("phase", ""),
                "desc": correction.get("description", ""),
                "obs": correction.get("observed_value") or "",
                "exp": correction.get("expected_value") or "",
                "conf": correction.get("confidence", 0.5),
                "status": correction.get("status", "staging"),
                "created": correction.get("created_at", ""),
            },
        )
    except Exception:
        pass  # already exists
    # Ensure AdvisorPhase node
    phase = correction.get("phase", "unknown")
    try:
        conn.execute("CREATE (p:AdvisorPhase {name: $name})", {"name": phase})
    except Exception:
        pass
    # FIXES edge
    try:
        conn.execute(
            "MATCH (c:Correction {id: $id}), (p:AdvisorPhase {name: $phase}) CREATE (c)-[:FIXES]->(p)",
            {"id": cid, "phase": phase},
        )
    except Exception:
        pass


def validate_correction_in_kg(correction_id: str, validated_by: str, validated_at: str, env: str = "production") -> None:
    if not _KUZU_AVAILABLE:
        return
    init_schema(env)
    _, conn = _open(env)
    try:
        conn.execute("MATCH (c:Correction {id: $id}) SET c.status = 'validated'", {"id": correction_id})
    except Exception:
        pass
    try:
        conn.execute("CREATE (h:Human {name: $name})", {"name": validated_by})
    except Exception:
        pass
    try:
        conn.execute(
            "MATCH (c:Correction {id: $id}), (h:Human {name: $by}) "
            "CREATE (c)-[:VALIDATED_BY {validated_at: $at}]->(h)",
            {"id": correction_id, "by": validated_by, "at": validated_at},
        )
    except Exception:
        pass


def get_validated_corrections(phase: str, env: str = "production") -> list[dict]:
    if not _KUZU_AVAILABLE:
        return []
    init_schema(env)
    _, conn = _open(env)
    try:
        result = conn.execute(
            "MATCH (c:Correction)-[:FIXES]->(p:AdvisorPhase {name: $phase}) "
            "WHERE c.status = 'validated' "
            "RETURN c.id, c.description, c.observed_value, c.expected_value, c.confidence "
            "ORDER BY c.confidence DESC LIMIT 10",
            {"phase": phase},
        )
        rows = []
        while result.has_next():
            row = result.get_next()
            rows.append({
                "id": row[0],
                "description": row[1],
                "observed_value": row[2],
                "expected_value": row[3],
                "confidence": row[4],
            })
        return rows
    except Exception as e:
        logger.warning("KG get_validated_corrections failed: %s", e)
        return []


def load_hardware_profiles_to_kg(profiles_path: Path, env: str = "production") -> int:
    """Seed KG from hardware_profiles.json. Returns number of TPS facts written."""
    if not _KUZU_AVAILABLE:
        return 0
    if not profiles_path.exists():
        return 0
    profiles = json.loads(profiles_path.read_text())
    count = 0
    for hw_name, hw_data in profiles.items():
        if hw_name.startswith("_"):
            continue
        ram = hw_data.get("hardware", {}).get("ram_gb", 0)
        hw_type = hw_data.get("hardware", {}).get("type", "unknown")
        for model_name, model_data in hw_data.get("models", {}).items():
            if isinstance(model_data, dict) and model_data.get("tokens_per_second"):
                upsert_hardware_tps(
                    hw_name=hw_name,
                    model_name=model_name,
                    tps=float(model_data["tokens_per_second"]),
                    ttft_ms=float(model_data.get("ttft_ms") or 0),
                    measured_at=model_data.get("measured_at", ""),
                    ram_gb=int(ram) if ram else 0,
                    hw_type=hw_type,
                    env=env,
                )
                count += 1
    return count


def is_available() -> bool:
    return _KUZU_AVAILABLE
