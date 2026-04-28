"""JSONL enrichment — inject rag_context into processed pairs before fine-tuning."""

from __future__ import annotations

import argparse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from api.db import execute
from pipeline.rag_index import RagIndex, SearchMode, build_index
from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.enrich")


def _load_project_config(project: str) -> dict[str, Any]:
    for candidate in (
        Path("config/models") / f"{project}.yaml",
        Path("config/projects") / f"{project}.yaml",
    ):
        if candidate.exists():
            with candidate.open() as fh:
                return yaml.safe_load(fh)
    raise FileNotFoundError(f"No config found for project '{project}'")


def _enrich_pair(
    pair: dict[str, Any],
    idx: RagIndex,
    k: int,
    mode: SearchMode,
    domain: str | None,
) -> dict[str, Any]:
    query = pair.get("prompt", pair.get("instruction", ""))
    if not query:
        return pair

    results = idx.search(query, k=k, domain_filter=domain, mode=mode)
    context = idx.format_context(results)
    if context:
        pair = {**pair, "rag_context": context}
    return pair


def enrich(
    project: str,
    *,
    input_path: Path | None = None,
    output_path: Path | None = None,
    k: int = 3,
    mode: SearchMode = "hybrid",
    rebuild_index: bool = False,
) -> dict[str, Any]:
    """
    Enrich processed JSONL pairs with RAG context.

    Returns stats dict: {n_input, n_enriched, n_skipped, output_path}.
    """
    cfg = _load_project_config(project)
    domain: str = cfg.get("domain", "")

    # Resolve paths
    if input_path is None:
        input_path = Path(cfg["data"]["processed"]).parent / "processed"
    if output_path is None:
        output_path = Path(cfg["data"].get("fine_tuning_ready", f"datalake/projects/{project}/fine-tuning-ready"))

    output_path.mkdir(parents=True, exist_ok=True)

    # Collect all processed JSONL files
    jsonl_files = sorted(input_path.glob("*.jsonl"))
    if not jsonl_files:
        logger.warning("No .jsonl files found in %s", input_path)
        return {"n_input": 0, "n_enriched": 0, "n_skipped": 0, "output_path": str(output_path)}

    # Build or reuse RAG index
    idx = build_index(project, domain, rebuild=rebuild_index)
    if idx.count == 0:
        logger.warning("RAG index is empty — pairs will have no rag_context")

    n_input = 0
    n_enriched = 0
    all_enriched: list[dict[str, Any]] = []

    for jsonl_file in jsonl_files:
        pairs = read_jsonl(jsonl_file)
        logger.info("Processing %s (%d pairs)", jsonl_file.name, len(pairs))
        for pair in pairs:
            n_input += 1
            enriched = _enrich_pair(pair, idx, k=k, mode=mode, domain=domain or None)
            if "rag_context" in enriched:
                n_enriched += 1
            all_enriched.append(enriched)

    n_skipped = n_input - n_enriched

    # Write output
    now_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_file = output_path / f"enriched_{now_tag}.jsonl"
    write_jsonl(out_file, all_enriched)

    # Log to SQLite
    dataset_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    execute(
        """INSERT OR IGNORE INTO datasets
           (id, project_id, stage, n_pairs, path, created_at)
           VALUES (?,?,?,?,?,?)""",
        (dataset_id, project, "fine-tuning-ready", n_input, str(out_file), now_iso),
    )

    logger.info(
        "Enrichment done — %d/%d pairs got rag_context (mode=%s, k=%d)",
        n_enriched, n_input, mode, k,
    )
    return {
        "n_input": n_input,
        "n_enriched": n_enriched,
        "n_skipped": n_skipped,
        "output_path": str(out_file),
        "rag_patterns_available": idx.count,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Enrich JSONL pairs with RAG context")
    parser.add_argument("project", help="Project ID")
    parser.add_argument("--input",  type=Path, default=None, help="Override input dir")
    parser.add_argument("--output", type=Path, default=None, help="Override output dir")
    parser.add_argument("--k",      type=int,  default=3,    help="Number of RAG results per pair")
    parser.add_argument("--mode",   default="hybrid",        choices=["vector", "fulltext", "hybrid"])
    parser.add_argument("--rebuild-index", action="store_true", help="Rebuild RAG index from scratch")
    args = parser.parse_args()

    stats = enrich(
        args.project,
        input_path=args.input,
        output_path=args.output,
        k=args.k,
        mode=args.mode,
        rebuild_index=args.rebuild_index,
    )
    print(f"Input pairs    : {stats['n_input']}")
    print(f"Enriched       : {stats['n_enriched']}")
    print(f"Skipped        : {stats['n_skipped']}")
    print(f"RAG patterns   : {stats['rag_patterns_available']}")
    print(f"Output         : {stats['output_path']}")


if __name__ == "__main__":
    _cli()
