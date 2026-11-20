"""Durable job ledger: every job and its events are stored in SQLite (standard library only).

One `jobs` row per job (status, executor, result) and an append-only `job_events` table
(log lines and status changes). Streams read events by `seq`, so nothing depends on process memory
and a restart loses nothing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.environ.get("MLOPS_DB", "experiments/mlops.db"))
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    project_id  TEXT,
    job_type    TEXT,
    executor    TEXT NOT NULL DEFAULT 'api',
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    finished_at REAL,
    returncode  INTEGER,
    output_file TEXT,
    result_json TEXT,
    meta_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id, created_at);
CREATE TABLE IF NOT EXISTS job_events (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT NOT NULL,
    ts        REAL NOT NULL,
    kind      TEXT NOT NULL,
    level     TEXT,
    message   TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON job_events(job_id, seq);
CREATE TABLE IF NOT EXISTS llm_calls (
    id             TEXT PRIMARY KEY,
    ts             REAL NOT NULL,
    project_id     TEXT,
    level          TEXT,
    purpose        TEXT,
    decision       TEXT NOT NULL,
    backend        TEXT,
    model          TEXT,
    entities_json  TEXT,
    sent_sha256    TEXT,
    sent_chars     INTEGER,
    sent_text      TEXT,
    ok             INTEGER,
    latency_ms     INTEGER,
    response_chars INTEGER,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_project ON llm_calls(project_id, ts);
"""

_LLM_CALL_COLUMNS = (
    "id", "ts", "project_id", "level", "purpose", "decision", "backend", "model",
    "entities_json", "sent_sha256", "sent_chars", "sent_text",
)


class RunLedger:
    """Thread-safe ledger on one SQLite file. Reuses a single connection (no open/close per log line)."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── writes ────────────────────────────────────────────────────────────────

    def create_job(
        self,
        job_id: str,
        project_id: str | None = None,
        job_type: str | None = None,
        *,
        status: str = "running",
        executor: str = "api",
        meta: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO jobs (id, project_id, job_type, executor, status, created_at, meta_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, project_id, job_type, executor, status, now, json.dumps(meta) if meta else None),
            )
            self._conn.execute(
                "INSERT INTO job_events (job_id, ts, kind, level, message) VALUES (?, ?, 'status', 'info', ?)",
                (job_id, now, status),
            )

    def log(self, job_id: str, message: str, level: str = "info") -> None:
        self.log_many(job_id, [message], level)

    def log_many(self, job_id: str, messages: list[str], level: str = "info") -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO job_events (job_id, ts, kind, level, message) VALUES (?, ?, 'log', ?, ?)",
                [(job_id, now, level, m) for m in messages],
            )

    def set_status(self, job_id: str, status: str) -> bool:
        """Change the status. A terminal job never changes again (a cancelled job stays cancelled).

        Returns True when the status changed.
        """
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATUSES or row["status"] == status:
                return False
            finished = now if status in TERMINAL_STATUSES else None
            self._conn.execute(
                "UPDATE jobs SET status = ?, finished_at = COALESCE(?, finished_at) WHERE id = ?",
                (status, finished, job_id),
            )
            self._conn.execute(
                "INSERT INTO job_events (job_id, ts, kind, level, message) VALUES (?, ?, 'status', 'info', ?)",
                (job_id, now, status),
            )
        return True

    def set_fields(
        self,
        job_id: str,
        *,
        returncode: int | None = None,
        output_file: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        sets, params = [], []
        if returncode is not None:
            sets.append("returncode = ?")
            params.append(returncode)
        if output_file is not None:
            sets.append("output_file = ?")
            params.append(output_file)
        if result is not None:
            sets.append("result_json = ?")
            params.append(json.dumps(result))
        if not sets:
            return
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", (*params, job_id))

    def interrupt_orphans(self, executor: str = "api") -> int:
        """Mark jobs still `running` for this executor as `interrupted` (their process is gone)."""
        with self._lock:
            ids = [
                r["id"]
                for r in self._conn.execute(
                    "SELECT id FROM jobs WHERE status IN ('running', 'pending') AND executor = ?", (executor,)
                )
            ]
        for job_id in ids:
            self.log(job_id, "Job interrupted: the API restarted before it finished.", level="warning")
            self.set_status(job_id, "interrupted")
        return len(ids)

    # ── reads ─────────────────────────────────────────────────────────────────

    def record_llm_call(self, call: dict[str, Any]) -> None:
        """Record an LLM call decision BEFORE it is sent (id chosen by the caller)."""
        values = [call.get(c) for c in _LLM_CALL_COLUMNS]
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO llm_calls ({', '.join(_LLM_CALL_COLUMNS)}) VALUES ({', '.join('?' * len(values))})",
                values,
            )

    def finish_llm_call(
        self, call_id: str, *, ok: bool, latency_ms: int | None = None,
        response_chars: int | None = None, error: str | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE llm_calls SET ok = ?, latency_ms = ?, response_chars = ?, error = ? WHERE id = ?",
                (int(ok), latency_ms, response_chars, error, call_id),
            )

    def list_llm_calls(self, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM llm_calls", []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["entities"] = json.loads(d.pop("entities_json")) if d.get("entities_json") else {}
            out.append(d)
        return out

    def exists(self, job_id: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            n_lines = self._conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE job_id = ? AND kind = 'log'", (job_id,)
            ).fetchone()[0]
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "job_type": row["job_type"],
            "executor": row["executor"],
            "status": row["status"],
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
            "returncode": row["returncode"],
            "output_file": row["output_file"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "meta": json.loads(row["meta_json"]) if row["meta_json"] else {},
            "n_lines": n_lines,
        }

    def events_since(self, job_id: str, last_seq: int = 0) -> list[tuple[int, str]]:
        """Log lines after `last_seq`, as (seq, message). Stream with the highest seq seen."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, message FROM job_events WHERE job_id = ? AND kind = 'log' AND seq > ? ORDER BY seq",
                (job_id, last_seq),
            ).fetchall()
        return [(r["seq"], r["message"]) for r in rows]

    def lines(self, job_id: str) -> list[str]:
        return [m for _, m in self.events_since(job_id)]

    def find_running(self, project_id: str, job_type: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM jobs WHERE project_id = ? AND job_type = ? AND status = 'running'"
                " ORDER BY created_at DESC LIMIT 1",
                (project_id, job_type),
            ).fetchone()
        return row["id"] if row else None

    def list_jobs(self, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT id FROM jobs", []
        if project_id:
            sql += " WHERE project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(sql, params)]
        return [j for j in (self.get_job(i) for i in ids) if j]


_LEDGERS: dict[Path, RunLedger] = {}
_LEDGERS_LOCK = threading.Lock()


def get_ledger(db_path: Path | str | None = None) -> RunLedger:
    """Process-wide ledger for a DB path (created on first use)."""
    path = Path(db_path) if db_path else DEFAULT_DB
    with _LEDGERS_LOCK:
        if path not in _LEDGERS:
            _LEDGERS[path] = RunLedger(path)
        return _LEDGERS[path]
