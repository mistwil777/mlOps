"""JSONL read/write utilities with schema validation and token counting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.jsonl")


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    records = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed JSON at %s:%d — %s", path, lineno, exc)
    return records


def write_jsonl(path: Path | str, data: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in data:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.info("Wrote %d records to %s", len(data), path)


def validate_jsonl_schema(
    path: Path | str,
    required_keys: list[str],
) -> tuple[bool, list[str]]:
    """Return (is_valid, list_of_error_messages)."""
    errors: list[str] = []
    records = read_jsonl(path)
    if not records:
        errors.append(f"{path}: file is empty")
        return False, errors

    for i, record in enumerate(records):
        missing = [k for k in required_keys if k not in record]
        if missing:
            errors.append(f"Record {i}: missing keys {missing}")

    return len(errors) == 0, errors


def count_tokens(text: str, model_name: str = "cl100k_base") -> int:
    """Count tokens using tiktoken (cl100k_base for Mistral/Qwen approximation)."""
    try:
        import tiktoken  # noqa: PLC0415

        enc = tiktoken.get_encoding(model_name)
        return len(enc.encode(text))
    except Exception:  # noqa: BLE001
        # Rough fallback: ~4 chars per token
        return len(text) // 4


def token_length_stats(path: Path | str) -> dict[str, float]:
    """Return token length statistics for all records in a JSONL file."""
    import statistics  # noqa: PLC0415

    records = read_jsonl(path)
    lengths = []
    for r in records:
        text = r.get("prompt", "") + r.get("completion", "")
        lengths.append(count_tokens(text))

    if not lengths:
        return {"count": 0, "min": 0, "max": 0, "mean": 0, "p95": 0}

    lengths_sorted = sorted(lengths)
    p95_idx = max(0, int(len(lengths_sorted) * 0.95) - 1)

    return {
        "count": len(lengths),
        "min": min(lengths),
        "max": max(lengths),
        "mean": round(statistics.mean(lengths), 1),
        "p95": lengths_sorted[p95_idx],
    }


def merge_jsonl(
    *paths: Path | str,
    output: Path | str,
    deduplicate: bool = True,
) -> int:
    """Merge multiple JSONL files, optionally deduplicating by (prompt, completion) hash."""
    all_records: list[dict[str, Any]] = []
    seen: set[int] = set()

    for p in paths:
        for record in read_jsonl(p):
            if deduplicate:
                key = hash(json.dumps(record, sort_keys=True))
                if key in seen:
                    continue
                seen.add(key)
            all_records.append(record)

    write_jsonl(output, all_records)
    return len(all_records)


def stats(path: Path | str) -> None:
    """CLI entry point: print token stats for a JSONL file."""
    s = token_length_stats(path)
    print(f"Records : {s['count']}")
    print(f"Tokens  : min={s['min']} max={s['max']} mean={s['mean']} p95={s['p95']}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python -m pipeline.utils.jsonl <command> <path>")
        print("Commands: stats, validate")
        sys.exit(1)

    cmd, fpath = sys.argv[1], sys.argv[2]
    if cmd == "stats":
        stats(fpath)
    elif cmd == "validate":
        keys = sys.argv[3:] if len(sys.argv) > 3 else ["prompt", "completion"]
        ok, errors = validate_jsonl_schema(fpath, keys)
        if ok:
            print("Schema valid")
        else:
            for e in errors:
                print(f"ERROR: {e}")
            sys.exit(1)
