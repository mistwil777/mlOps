"""QA test run persistence — SQLite store.

Schema:
  qa_runs        — one row per test session (CDC + tester + date + git hash)
  qa_check_results — one row per individual checkbox result
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path("experiments/qa/qa_store.db")


def _git_hash() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return None


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS qa_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date    TEXT    NOT NULL,
            git_hash    TEXT,
            cdc_id      TEXT    NOT NULL,
            cdc_version TEXT,
            tester      TEXT,
            total       INTEGER NOT NULL DEFAULT 0,
            passed      INTEGER NOT NULL DEFAULT 0,
            failed      INTEGER NOT NULL DEFAULT 0,
            skipped     INTEGER NOT NULL DEFAULT 0,
            notes       TEXT,
            duration_s  REAL
        );

        CREATE TABLE IF NOT EXISTS qa_check_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL REFERENCES qa_runs(id) ON DELETE CASCADE,
            scenario_id TEXT    NOT NULL,
            check_index INTEGER NOT NULL,
            label       TEXT    NOT NULL,
            status      TEXT    NOT NULL CHECK(status IN ('pass','fail','skip')),
            notes       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_runs_cdc    ON qa_runs(cdc_id);
        CREATE INDEX IF NOT EXISTS idx_runs_date   ON qa_runs(run_date);
        CREATE INDEX IF NOT EXISTS idx_checks_run  ON qa_check_results(run_id);
        CREATE INDEX IF NOT EXISTS idx_checks_scen ON qa_check_results(scenario_id);
    """)


@contextmanager
def _conn():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    _migrate(con)
    try:
        yield con
        con.commit()
    finally:
        con.close()


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    scenario_id: str
    check_index: int
    label: str
    status: str          # "pass" | "fail" | "skip"
    notes: str = ""


@dataclass
class RunPayload:
    cdc_id: str
    checks: list[CheckResult]
    tester: str = ""
    notes: str = ""
    cdc_version: str = ""
    duration_s: float | None = None


def save_run(payload: RunPayload) -> int:
    passed  = sum(1 for c in payload.checks if c.status == "pass")
    failed  = sum(1 for c in payload.checks if c.status == "fail")
    skipped = sum(1 for c in payload.checks if c.status == "skip")

    with _conn() as con:
        cur = con.execute(
            """INSERT INTO qa_runs
               (run_date, git_hash, cdc_id, cdc_version, tester,
                total, passed, failed, skipped, notes, duration_s)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                _git_hash(),
                payload.cdc_id,
                payload.cdc_version or None,
                payload.tester or None,
                len(payload.checks),
                passed, failed, skipped,
                payload.notes or None,
                payload.duration_s,
            ),
        )
        run_id = cur.lastrowid
        con.executemany(
            """INSERT INTO qa_check_results
               (run_id, scenario_id, check_index, label, status, notes)
               VALUES (?,?,?,?,?,?)""",
            [
                (run_id, c.scenario_id, c.check_index, c.label, c.status, c.notes or None)
                for c in payload.checks
            ],
        )
    return run_id


def get_runs(cdc_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with _conn() as con:
        if cdc_id:
            rows = con.execute(
                "SELECT * FROM qa_runs WHERE cdc_id=? ORDER BY run_date DESC LIMIT ?",
                (cdc_id, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM qa_runs ORDER BY run_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_run_detail(run_id: int) -> dict[str, Any] | None:
    with _conn() as con:
        run = con.execute("SELECT * FROM qa_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        checks = con.execute(
            "SELECT * FROM qa_check_results WHERE run_id=? ORDER BY scenario_id, check_index",
            (run_id,),
        ).fetchall()
    return {**dict(run), "checks": [dict(c) for c in checks]}


def get_failure_analysis(cdc_id: str | None = None) -> list[dict[str, Any]]:
    """Return checks ranked by failure rate — identifies systematic weaknesses."""
    with _conn() as con:
        where = "WHERE r.cdc_id=?" if cdc_id else ""
        params = (cdc_id,) if cdc_id else ()
        rows = con.execute(
            f"""
            SELECT
                c.scenario_id,
                c.label,
                COUNT(*) AS total_runs,
                SUM(CASE WHEN c.status='fail' THEN 1 ELSE 0 END) AS failures,
                SUM(CASE WHEN c.status='pass' THEN 1 ELSE 0 END) AS passes,
                ROUND(100.0 * SUM(CASE WHEN c.status='fail' THEN 1 ELSE 0 END) / COUNT(*), 1) AS failure_rate
            FROM qa_check_results c
            JOIN qa_runs r ON r.id = c.run_id
            {where}
            GROUP BY c.scenario_id, c.label
            ORDER BY failure_rate DESC, total_runs DESC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_trend(cdc_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Pass rate over time for a CDC — detects regression."""
    with _conn() as con:
        rows = con.execute(
            """SELECT run_date, git_hash, passed, failed, skipped, total,
                      ROUND(100.0 * passed / NULLIF(total,0), 1) AS pass_rate
               FROM qa_runs WHERE cdc_id=?
               ORDER BY run_date DESC LIMIT ?""",
            (cdc_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
