"""Multi-LoRA adapter registry — track adapters, route requests, store data→score links."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.db import execute, fetchall, fetchone

router = APIRouter(prefix="/api/adapters", tags=["adapters"])


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_schema() -> None:
    """Create adapter-related tables if they don't exist."""
    execute("""CREATE TABLE IF NOT EXISTS adapters (
        id              TEXT PRIMARY KEY,
        project_id      TEXT NOT NULL,
        run_id          TEXT,
        name            TEXT NOT NULL,
        domain          TEXT,
        task_type       TEXT,
        adapter_path    TEXT,
        base_model      TEXT,
        val_loss        REAL,
        rouge_l         REAL,
        pass_rate       REAL,
        n_train_pairs   INTEGER,
        avg_data_quality REAL,
        status          TEXT DEFAULT 'ready',
        created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        notes           TEXT
    )""")

    execute("""CREATE TABLE IF NOT EXISTS adapter_data_links (
        id              TEXT PRIMARY KEY,
        adapter_id      TEXT REFERENCES adapters(id),
        data_source     TEXT,
        n_pairs         INTEGER,
        avg_quality     REAL,
        domain          TEXT,
        task_type       TEXT,
        prompt_token_p50 REAL,
        completion_token_p50 REAL,
        created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    )""")


class AdapterCreate(BaseModel):
    project_id: str
    run_id: str | None = None
    name: str
    domain: str
    task_type: str
    adapter_path: str
    base_model: str
    val_loss: float | None = None
    rouge_l: float | None = None
    pass_rate: float | None = None
    n_train_pairs: int | None = None
    avg_data_quality: float | None = None
    notes: str | None = None


class DataLinkCreate(BaseModel):
    data_source: str
    n_pairs: int
    avg_quality: float | None = None
    domain: str | None = None
    task_type: str | None = None
    prompt_token_p50: float | None = None
    completion_token_p50: float | None = None


@router.get("")
def list_adapters(project_id: str | None = None) -> list[dict[str, Any]]:
    _ensure_schema()
    sql = "SELECT * FROM adapters"
    params: tuple = ()
    if project_id:
        sql += " WHERE project_id = ?"
        params = (project_id,)
    sql += " ORDER BY created_at DESC"
    return fetchall(sql, params)


@router.get("/{adapter_id}")
def get_adapter(adapter_id: str) -> dict[str, Any]:
    _ensure_schema()
    row = fetchone("SELECT * FROM adapters WHERE id = ?", (adapter_id,))
    if not row:
        raise HTTPException(404, f"Adapter '{adapter_id}' not found")
    row["data_links"] = fetchall(
        "SELECT * FROM adapter_data_links WHERE adapter_id = ? ORDER BY created_at DESC",
        (adapter_id,),
    )
    return row


@router.post("", status_code=201)
def register_adapter(body: AdapterCreate) -> dict[str, str]:
    _ensure_schema()
    adapter_id = str(uuid.uuid4())[:8]
    execute(
        """INSERT INTO adapters
           (id,project_id,run_id,name,domain,task_type,adapter_path,base_model,
            val_loss,rouge_l,pass_rate,n_train_pairs,avg_data_quality,notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (adapter_id, body.project_id, body.run_id, body.name, body.domain, body.task_type,
         body.adapter_path, body.base_model, body.val_loss, body.rouge_l, body.pass_rate,
         body.n_train_pairs, body.avg_data_quality, body.notes),
    )
    return {"adapter_id": adapter_id}


@router.delete("/{adapter_id}")
def delete_adapter(adapter_id: str) -> dict[str, str]:
    _ensure_schema()
    execute("DELETE FROM adapter_data_links WHERE adapter_id = ?", (adapter_id,))
    execute("DELETE FROM adapters WHERE id = ?", (adapter_id,))
    return {"ok": "deleted"}


@router.post("/{adapter_id}/data-links", status_code=201)
def add_data_link(adapter_id: str, body: DataLinkCreate) -> dict[str, str]:
    """Record which dataset characteristics produced which adapter scores."""
    _ensure_schema()
    row = fetchone("SELECT id FROM adapters WHERE id = ?", (adapter_id,))
    if not row:
        raise HTTPException(404, f"Adapter '{adapter_id}' not found")

    link_id = str(uuid.uuid4())[:8]
    execute(
        """INSERT INTO adapter_data_links
           (id,adapter_id,data_source,n_pairs,avg_quality,domain,task_type,
            prompt_token_p50,completion_token_p50)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (link_id, adapter_id, body.data_source, body.n_pairs, body.avg_quality,
         body.domain, body.task_type, body.prompt_token_p50, body.completion_token_p50),
    )
    return {"link_id": link_id}


@router.get("/{adapter_id}/data-links")
def get_data_links(adapter_id: str) -> list[dict[str, Any]]:
    _ensure_schema()
    return fetchall(
        "SELECT * FROM adapter_data_links WHERE adapter_id = ? ORDER BY created_at DESC",
        (adapter_id,),
    )


@router.post("/route")
def route_request(domain: str, task_type: str, project_id: str | None = None) -> dict[str, Any]:
    """Find the best adapter for a given domain/task_type combination."""
    _ensure_schema()
    sql = """SELECT * FROM adapters WHERE domain = ? AND task_type = ? AND status = 'ready'"""
    params: list = [domain, task_type]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    # Best adapter = lowest val_loss (prefer ROUGE-L as tiebreaker)
    sql += " ORDER BY val_loss ASC, rouge_l DESC LIMIT 1"
    row = fetchone(sql, tuple(params))
    if not row:
        raise HTTPException(404, f"No adapter found for domain={domain} task_type={task_type}")
    return row
