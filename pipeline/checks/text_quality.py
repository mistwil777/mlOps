"""Heuristic text quality filter — length, entropy, boilerplate detection.

Catches: empty fields, Lorem Ipsum, nav/sidebar text scraped by accident,
excessively long records that exceed fine-tuning context windows.

CLI:
    python -m pipeline.checks.text_quality --input <file.jsonl> [--output <out.jsonl>]
    python -m pipeline.checks.text_quality --text "some text"
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.checks.text_quality")

_BOILERPLATE_PATTERNS = [
    re.compile(r"lorem ipsum", re.IGNORECASE),
    re.compile(r"click here to"),
    re.compile(r"all rights reserved", re.IGNORECASE),
    re.compile(r"^(home|about|contact|search|login|sign in|sign up|menu)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"cookie(s)? policy", re.IGNORECASE),
    re.compile(r"subscribe to our newsletter", re.IGNORECASE),
    re.compile(r"^\s*(next|previous|back|skip)\s*$", re.IGNORECASE | re.MULTILINE),
]

_DEFAULT_MIN_CHARS   = 80
_DEFAULT_MAX_CHARS   = 8000
_DEFAULT_MIN_ENTROPY = 3.0


def _char_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    total  = len(text)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def check_text(
    text: str,
    min_chars: int   = _DEFAULT_MIN_CHARS,
    max_chars: int   = _DEFAULT_MAX_CHARS,
    min_entropy: float = _DEFAULT_MIN_ENTROPY,
) -> tuple[bool, str]:
    """Return (is_valid, reason)."""
    stripped = text.strip()

    if len(stripped) < min_chars:
        return False, f"too_short ({len(stripped)} < {min_chars})"
    if len(stripped) > max_chars:
        return False, f"too_long ({len(stripped)} > {max_chars})"

    entropy = _char_entropy(stripped)
    if entropy < min_entropy:
        return False, f"low_entropy ({entropy:.2f} < {min_entropy})"

    for pattern in _BOILERPLATE_PATTERNS:
        if pattern.search(stripped):
            return False, f"boilerplate ({pattern.pattern[:40]})"

    return True, "ok"


def check_record(
    record: dict[str, Any],
    min_chars: int     = _DEFAULT_MIN_CHARS,
    max_chars: int     = _DEFAULT_MAX_CHARS,
    min_entropy: float = _DEFAULT_MIN_ENTROPY,
) -> tuple[bool, str]:
    """Check quality of the primary text fields in a raw record."""
    # Determine which fields to check based on schema
    if "task_name" in record and "code" in record:
        primary = str(record.get("code", ""))
    elif "description" in record:
        primary = str(record.get("description", ""))
    elif "prompt" in record:
        primary = str(record.get("prompt", ""))
    else:
        return True, "no_text_field"

    return check_text(primary, min_chars, max_chars, min_entropy)


def filter_dataset(
    input_path: Path | str,
    output_path: Path | str | None = None,
    min_chars: int     = _DEFAULT_MIN_CHARS,
    max_chars: int     = _DEFAULT_MAX_CHARS,
    min_entropy: float = _DEFAULT_MIN_ENTROPY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter JSONL file. Return (passed, rejected)."""
    records  = read_jsonl(input_path)
    passed:   list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for r in records:
        ok, reason = check_record(r, min_chars, max_chars, min_entropy)
        if ok:
            passed.append(r)
        else:
            rejected.append({**r, "_quality_reject_reason": reason})
            logger.debug("Rejected (quality): %s", reason)

    logger.info(
        "Text quality: %d passed / %d rejected (%.1f%% pass rate)",
        len(passed), len(rejected),
        100 * len(passed) / len(records) if records else 0,
    )

    if output_path and passed:
        write_jsonl(output_path, passed)

    return passed, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="Heuristic text quality filter")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", help="JSONL file to filter")
    group.add_argument("--text",  help="Single text to check")
    parser.add_argument("--output",      help="Output JSONL (passed records only)")
    parser.add_argument("--min-chars",   type=int,   default=_DEFAULT_MIN_CHARS)
    parser.add_argument("--max-chars",   type=int,   default=_DEFAULT_MAX_CHARS)
    parser.add_argument("--min-entropy", type=float, default=_DEFAULT_MIN_ENTROPY)
    args = parser.parse_args()

    if args.text:
        ok, reason = check_text(args.text, args.min_chars, args.max_chars, args.min_entropy)
        print(f"{'OK' if ok else 'FAIL'}: {reason}")
    else:
        passed, rejected = filter_dataset(
            args.input, args.output,
            args.min_chars, args.max_chars, args.min_entropy,
        )
        if not args.output:
            print(f"Passed: {len(passed)}  Rejected: {len(rejected)}")


if __name__ == "__main__":
    main()
