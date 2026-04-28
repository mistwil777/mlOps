"""Sol feedback pipeline — ingest validated entries into RAG + check retrain threshold."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from api.db import execute, fetchone
from pipeline.rag_index import RagIndex
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.feedback")

_FEEDBACK_SCHEMA_REQUIRED = {"content", "domain"}


def _load_project_config(project: str) -> dict[str, Any]:
    for candidate in (
        Path("config/models") / f"{project}.yaml",
        Path("config/projects") / f"{project}.yaml",
    ):
        if candidate.exists():
            with candidate.open() as fh:
                return yaml.safe_load(fh)
    raise FileNotFoundError(f"No config found for project '{project}'")


def _ensure_rag_feedback_table() -> None:
    execute("""
        CREATE TABLE IF NOT EXISTS rag_feedback (
            id              TEXT PRIMARY KEY,
            project_id      TEXT,
            content         TEXT NOT NULL,
            domain          TEXT,
            category        TEXT,
            quality_label   TEXT DEFAULT 'validated',
            validated_by    TEXT,
            ingested_into_rag INTEGER DEFAULT 0,
            rag_corpus_version TEXT,
            created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)


def _count_pending_feedback(project: str) -> int:
    row = fetchone(
        "SELECT COUNT(*) as n FROM rag_feedback WHERE project_id = ? AND ingested_into_rag = 0",
        (project,),
    )
    return (row or {}).get("n", 0)


def _count_total_feedback_since_last_training(project: str) -> int:
    """Count feedback entries added since the last completed training run."""
    row = fetchone(
        """SELECT COUNT(*) as n FROM rag_feedback
           WHERE project_id = ?
           AND created_at > COALESCE(
               (SELECT finished_at FROM runs
                WHERE project_id = ? AND status = 'completed'
                ORDER BY finished_at DESC LIMIT 1),
               '1970-01-01T00:00:00Z'
           )""",
        (project, project),
    )
    return (row or {}).get("n", 0)


def _trigger_pending_retrain(project: str) -> None:
    """Insert a pending_retrain sentinel run into SQLite."""
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    execute(
        """INSERT OR IGNORE INTO runs
           (id, project_id, experiment_name, model_name, status, started_at)
           VALUES (?,?,?,?,?,?)""",
        (run_id, project, "retrain-trigger", "pending", "pending_retrain", now),
    )
    logger.info("Retrain threshold reached for '%s' — run %s flagged as pending_retrain", project, run_id)


def ingest_entry(
    project: str,
    entry: dict[str, Any],
    *,
    write_to_rag: bool = True,
) -> str:
    """
    Ingest a single validated feedback entry.

    Returns the UUID of the created rag_feedback row.
    Optionally writes directly to LanceDB (write_to_rag=True).
    """
    _ensure_rag_feedback_table()

    missing = _FEEDBACK_SCHEMA_REQUIRED - entry.keys()
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    entry_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    execute(
        """INSERT INTO rag_feedback
           (id, project_id, content, domain, category, quality_label, validated_by, ingested_into_rag, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            entry_id, project,
            entry["content"], entry.get("domain", ""),
            entry.get("category", "feedback"),
            entry.get("quality_label", "validated"),
            entry.get("validated_by", ""),
            0, now,
        ),
    )

    if write_to_rag:
        idx = RagIndex(project)
        idx.ingest_feedback(entry)
        execute(
            "UPDATE rag_feedback SET ingested_into_rag = 1 WHERE id = ?",
            (entry_id,),
        )
        logger.info("Feedback entry %s ingested into RAG (project=%s)", entry_id, project)

    return entry_id


def ingest_feedback_dir(
    project: str,
    feedback_dir: Path | None = None,
    *,
    write_to_rag: bool = True,
) -> dict[str, Any]:
    """
    Scan datalake/projects/<project>/raw/feedback/*.json and ingest all entries.

    Each file must be a JSON array of feedback objects or a single object.
    Returns stats: {n_files, n_entries, n_ingested, retrain_triggered}.
    """
    _ensure_rag_feedback_table()
    cfg = _load_project_config(project)

    if feedback_dir is None:
        feedback_dir = Path(f"datalake/projects/{project}/raw/feedback")

    if not feedback_dir.exists():
        logger.info("No feedback dir at %s — nothing to ingest", feedback_dir)
        return {"n_files": 0, "n_entries": 0, "n_ingested": 0, "retrain_triggered": False}

    json_files = sorted(feedback_dir.glob("*.json"))
    n_files = 0
    n_entries = 0
    n_ingested = 0

    for json_file in json_files:
        n_files += 1
        with json_file.open(encoding="utf-8") as fh:
            raw = json.load(fh)

        entries = raw if isinstance(raw, list) else [raw]
        for entry in entries:
            n_entries += 1
            try:
                ingest_entry(project, entry, write_to_rag=write_to_rag)
                n_ingested += 1
            except Exception as exc:
                logger.warning("Skipping entry in %s: %s", json_file.name, exc)

        # Archive processed file
        done_dir = feedback_dir / "done"
        done_dir.mkdir(exist_ok=True)
        json_file.rename(done_dir / json_file.name)

    # Check retrain threshold
    threshold = cfg.get("retrain_threshold", {})
    min_new_patterns: int = threshold.get("new_patterns", 200)
    new_since_last = _count_total_feedback_since_last_training(project)
    retrain_triggered = False

    if new_since_last >= min_new_patterns:
        _trigger_pending_retrain(project)
        retrain_triggered = True

    logger.info(
        "Feedback ingest done — %d files, %d entries, %d ingested, retrain=%s",
        n_files, n_entries, n_ingested, retrain_triggered,
    )
    return {
        "n_files":          n_files,
        "n_entries":        n_entries,
        "n_ingested":       n_ingested,
        "new_since_last_training": new_since_last,
        "retrain_threshold": min_new_patterns,
        "retrain_triggered": retrain_triggered,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Ingest sol feedback into RAG")
    parser.add_argument("project", help="Project ID")
    parser.add_argument("--dir",       type=Path,  default=None, help="Override feedback directory")
    parser.add_argument("--no-rag",    action="store_true",      help="Store in SQLite only, skip LanceDB write")
    parser.add_argument("--entry",     type=str,   default=None, help="Single JSON entry (inline)")
    args = parser.parse_args()

    if args.entry:
        entry = json.loads(args.entry)
        entry_id = ingest_entry(args.project, entry, write_to_rag=not args.no_rag)
        print(f"Ingested: {entry_id}")
    else:
        stats = ingest_feedback_dir(args.project, args.dir, write_to_rag=not args.no_rag)
        for k, v in stats.items():
            print(f"{k:30s}: {v}")


if __name__ == "__main__":
    _cli()
