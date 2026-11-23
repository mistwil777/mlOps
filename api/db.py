"""SQLite connection helpers — no ORM, raw sqlite3."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

DB_PATH = Path("experiments/mlops.db")
_PROJECT_DB_DIR = Path("experiments/projects")

_PROJECT_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CENTRAL_MIGRATIONS = [
    "ALTER TABLE projects ADD COLUMN classification TEXT DEFAULT 'interne'",
    "ALTER TABLE projects ADD COLUMN domain TEXT DEFAULT 'other'",
    "ALTER TABLE projects ADD COLUMN task_type TEXT DEFAULT 'generation'",
    "ALTER TABLE projects ADD COLUMN config_json TEXT DEFAULT '{}'",
]


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_central_db() -> None:
    """Add missing columns to central DB — idempotent."""
    with get_db() as conn:
        for stmt in _CENTRAL_MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


def ensure_project_db(project_id: str) -> Path:
    """Create per-project SQLite DB if it does not exist. Returns path."""
    db_dir = _PROJECT_DB_DIR / project_id
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "mlops.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_PROJECT_DB_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


@contextmanager
def get_project_db(project_id: str) -> Generator[sqlite3.Connection, None, None]:
    db_path = _PROJECT_DB_DIR / project_id / "mlops.db"
    if not db_path.exists():
        ensure_project_db(project_id)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetchone(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    with get_db() as conn:
        cur = conn.execute(sql, params)
    return cur.lastrowid or 0


def project_config_get(project_id: str) -> dict[str, Any] | None:
    """Read full project config from central DB config_json column.

    Fallback chain:
    1. Central DB config_json
    2. Per-project DB mirror (central DB corrupt/missing)
    3. Auto-reconstruct adapters from datalake chunks (config present but adapters lost)
    """
    row = fetchone(
        "SELECT config_json, name, classification, domain, task_type FROM projects WHERE id = ?",
        (project_id,),
    )
    if not row:
        # Try per-project DB mirror before giving up
        cfg = _project_config_from_local_db(project_id)
        if cfg:
            project_config_save(project_id, cfg)  # re-populate central DB
        return cfg

    cfg = json.loads(row["config_json"] or "{}")
    cfg.setdefault("id", project_id)
    cfg.setdefault("name", row["name"])
    cfg.setdefault("classification", row["classification"])
    cfg.setdefault("domain", row["domain"])
    cfg.setdefault("task_type", row["task_type"])

    # Auto-reconstruct adapters from datalake if lost
    if not cfg.get("adapters"):
        reconstructed = _reconstruct_adapters_from_chunks(project_id)
        if reconstructed:
            cfg["adapters"] = reconstructed
            project_config_save(project_id, cfg)

    return cfg


def _project_config_from_local_db(project_id: str) -> dict[str, Any] | None:
    """Read config from per-project DB mirror."""
    db_path = _PROJECT_DB_DIR / project_id / "mlops.db"
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        row = conn.execute("SELECT value FROM project_config WHERE key='config'").fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _reconstruct_adapters_from_chunks(project_id: str) -> list[dict[str, Any]]:
    """Build adapter list from adapter_name fields found in raw/ JSONL chunks."""
    raw_dir = Path("datalake/projects") / project_id / "raw"
    if not raw_dir.exists():
        return []
    seen: dict[str, dict[str, Any]] = {}
    for f in raw_dir.glob("*.jsonl"):
        try:
            with f.open() as fh:
                for line in fh:
                    r = json.loads(line)
                    name = r.get("adapter_name")
                    if name and name not in seen:
                        seen[name] = {
                            "name": name,
                            "role": "orchestrator" if "orchestrat" in name else "specialist",
                            "description": "",
                            "estimated_pairs": 50,
                            "pair_strategy": "chunk_to_pair",
                            "_auto_reconstructed": True,
                        }
        except Exception:
            continue
    return list(seen.values())


def project_config_list() -> list[dict[str, Any]]:
    """List all projects from central DB."""
    rows = fetchall("SELECT id, config_json, name, classification, domain, task_type FROM projects ORDER BY id")
    result = []
    for row in rows:
        cfg = json.loads(row["config_json"] or "{}")
        cfg.setdefault("id", row["id"])
        cfg.setdefault("name", row["name"])
        cfg.setdefault("classification", row["classification"])
        cfg.setdefault("domain", row["domain"])
        cfg.setdefault("task_type", row["task_type"])
        result.append(cfg)
    return result


def project_config_save(project_id: str, cfg: dict[str, Any]) -> None:
    """Upsert project config into central DB and mirror into per-project DB."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = json.dumps(cfg, ensure_ascii=False)

    # Central registry
    execute(
        """INSERT INTO projects (id, name, description, base_model, classification, domain, task_type, config_json, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name,
               description=excluded.description,
               base_model=excluded.base_model,
               classification=excluded.classification,
               domain=excluded.domain,
               task_type=excluded.task_type,
               config_json=excluded.config_json,
               updated_at=excluded.updated_at""",
        (
            project_id,
            cfg.get("name", project_id),
            cfg.get("description", ""),
            cfg.get("model", ""),
            cfg.get("classification", "interne"),
            cfg.get("domain", "other"),
            cfg.get("task_type", "generation"),
            payload,
            now,
            now,
        ),
    )

    # Per-project DB mirror — source of truth for the client bundle
    db_path = ensure_project_db(project_id)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO project_config (key, value) VALUES ('config', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (payload,),
    )
    conn.commit()
    conn.close()
