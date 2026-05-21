"""Two-level deduplication pipeline.

Level 1 — exact hash: SHA-256 of normalised field value → instant O(1) match.
Level 2 — near-duplicate: embedding cosine distance ≥ threshold → semantic duplicate.

Always runs before any LLM verifier (free cost, maximum GIGO protection).

Reads config/verifiers/registry.yaml for default thresholds.

CLI:
    python -m pipeline.deduplicate --project <id>
    python -m pipeline.deduplicate --input <file.jsonl> --output <out.jsonl> [--field code] [--threshold 0.97]
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.deduplicate")

_REGISTRY_PATH = Path("config/verifiers/registry.yaml")
_PROJECTS_DIR  = Path("config/projects")


def _load_dedup_config() -> dict[str, Any]:
    try:
        with _REGISTRY_PATH.open() as fh:
            reg = yaml.safe_load(fh) or {}
        return reg.get("verifiers", {}).get("deduplication", {}).get("params", {})
    except FileNotFoundError:
        return {}


def _normalise(value: str) -> str:
    """Normalise text for exact-hash comparison (whitespace collapse, lowercase)."""
    return " ".join(value.lower().split())


def _field_hash(record: dict[str, Any], field: str) -> str | None:
    raw = record.get(field)
    if raw is None:
        return None
    return hashlib.sha256(_normalise(str(raw)).encode()).hexdigest()


def dedup_exact(
    records: list[dict[str, Any]],
    field: str = "code",
) -> tuple[list[dict[str, Any]], int]:
    """Remove exact duplicates based on SHA-256 hash of normalised field.

    Returns (deduplicated_records, n_removed).
    Falls back to hashing the full record if field is absent in all records.
    """
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    removed = 0

    for r in records:
        key = _field_hash(r, field)
        if key is None:
            # Field absent — hash full record
            key = hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()
        if key in seen:
            removed += 1
        else:
            seen.add(key)
            result.append(r)

    logger.info("Exact dedup (field=%s): %d removed", field, removed)
    return result, removed


def run(
    input_path: Path | str,
    output_path: Path | str,
    field: str | None = None,
    near_dup_threshold: float | None = None,
) -> dict[str, int]:
    """Full two-level deduplication.

    Returns stats dict with exact_removed, near_removed, final_count.
    """
    cfg = _load_dedup_config()
    eff_field     = field             or cfg.get("exact_field", "code")
    eff_threshold = near_dup_threshold or cfg.get("near_dup_threshold", 0.97)

    records = read_jsonl(input_path)
    logger.info("Dedup: starting with %d records", len(records))

    # Level 1 — exact
    after_exact, exact_removed = dedup_exact(records, eff_field)

    # Level 2 — near-duplicate via embeddings
    from pipeline.checks.similarity import find_near_duplicates  # noqa: PLC0415

    dup_pairs    = find_near_duplicates(after_exact, eff_field, eff_threshold)
    dup_idx      = {b for _, b, _ in dup_pairs}
    after_near   = [r for i, r in enumerate(after_exact) if i not in dup_idx]
    near_removed = len(after_exact) - len(after_near)
    logger.info("Near-dup dedup (threshold=%.2f): %d removed", eff_threshold, near_removed)

    write_jsonl(output_path, after_near)

    stats = {
        "input":        len(records),
        "exact_removed": exact_removed,
        "near_removed":  near_removed,
        "final_count":   len(after_near),
    }
    logger.info(
        "Dedup complete: %d → %d (-%d exact, -%d near-dup)",
        stats["input"], stats["final_count"], exact_removed, near_removed,
    )
    return stats


def run_for_project(project_id: str) -> dict[str, int]:
    """Convenience wrapper: dedup the consolidated raw file for a project."""
    base = Path(f"datalake/projects/{project_id}/raw")
    input_path  = base / "consolidated.jsonl"
    output_path = base / "consolidated_deduped.jsonl"

    if not input_path.exists():
        raise FileNotFoundError(
            f"{input_path} not found. Run data collection first, "
            "then call /api/data/consolidate to merge raw files."
        )

    return run(input_path, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-level JSONL deduplication")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project ID (uses datalake/projects/<id>/raw/)")
    group.add_argument("--input",   help="Input JSONL file")
    parser.add_argument("--output",    help="Output JSONL (required with --input)")
    parser.add_argument("--field",     help="Field to hash/embed (default: code)")
    parser.add_argument("--threshold", type=float, help="Near-dup cosine threshold (default: 0.97)")
    args = parser.parse_args()

    if args.project:
        stats = run_for_project(args.project)
    else:
        if not args.output:
            parser.error("--output required with --input")
        stats = run(args.input, args.output, args.field, args.threshold)

    print(
        f"Input: {stats['input']}  "
        f"Exact removed: {stats['exact_removed']}  "
        f"Near-dup removed: {stats['near_removed']}  "
        f"Final: {stats['final_count']}"
    )


if __name__ == "__main__":
    main()
