"""Fixer: Java split(sep, N) → Python split(sep, N-1).

Java: N = max number of PARTS  →  "a,b,c,d".split(",", 3) = ["a", "b", "c,d"]  (3 parts)
Python: N = max number of SPLITS → "a,b,c,d".split(",", 3) = ["a", "b", "c", "d"]  (3 splits = 4 parts)
Correct translation: Java split(sep, N) → Python split(sep, N-1).
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.fixers.base import FixResult, Fixer

# Detect Java split with a numeric limit in the prompt
_JAVA_SPLIT_RE = re.compile(r'\.split\s*\(\s*(["\'][^"\']*["\']|[a-zA-Z_]\w*)\s*,\s*(\d+)\s*\)')

# Find Python split with a numeric limit in the completion (capture the number)
_PY_SPLIT_RE = re.compile(r'(\.split\s*\(\s*(?:["\'][^"\']*["\']|[a-zA-Z_]\w*)\s*,\s*)(\d+)(\s*\))')


def _java_split_limits(prompt: str) -> set[int]:
    """Return all N values found in Java split(sep, N) calls in the prompt."""
    return {int(m.group(2)) for m in _JAVA_SPLIT_RE.finditer(prompt)}


def fix_split_semantics(prompt: str, completion: str) -> tuple[str, list[str]]:
    """Decrement split limit in Python completion where Java prompt had split(sep, N).

    Returns (fixed_completion, list_of_changes).
    """
    java_limits = _java_split_limits(prompt)
    if not java_limits:
        return completion, []

    changes: list[str] = []
    fixed = completion

    def _replace(m: re.Match) -> str:
        n = int(m.group(2))
        if n in java_limits and n >= 2:
            new_n = n - 1
            changes.append(f"split limit {n} → {new_n} (Java N parts → Python N-1 splits)")
            return m.group(1) + str(new_n) + m.group(3)
        return m.group(0)

    fixed = _PY_SPLIT_RE.sub(_replace, fixed)
    return fixed, changes


class SplitSemanticsFixer(Fixer):
    reason = "split_semantics"

    def applies_to(self, record: dict[str, Any], stage: str) -> bool:
        lang_pair = str(record.get("lang_pair") or "")
        return stage == "pairs" and "java" in lang_pair.lower()

    def fix(self, record: dict[str, Any]) -> FixResult:
        prompt = record.get("prompt") or ""
        completion = record.get("completion") or ""
        if not prompt or not completion:
            return FixResult(record=record, fixed=False)
        fixed, changes = fix_split_semantics(prompt, completion)
        if not changes:
            return FixResult(record=record, fixed=False)
        updated = {**record, "completion": fixed, "_split_semantics_fixes": changes}
        return FixResult(record=updated, fixed=True, changes=changes)
