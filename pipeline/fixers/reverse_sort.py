"""Fixer: Java reverse comparator → Python sort(reverse=True).

Java patterns detected in prompt:
  - (s1, s2) -> s2.compareTo(s1)  or  (a, b) -> b.compareTo(a)
  - Collections.reverseOrder()
  - Comparator.reverseOrder()

Python patterns checked in completion:
  - sorted(..., reverse=True)
  - .sort(reverse=True)
  - .sort(key=..., reverse=True)

If Java prompt has a reverse comparator and Python completion has sort() without reverse=True,
inject reverse=True.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.fixers.base import FixResult, Fixer

_JAVA_REVERSE_PATTERNS = [
    re.compile(r'\(\s*\w+\s*,\s*\w+\s*\)\s*->\s*\w+\.compareTo\(\w+\)'),  # (s1,s2)->s2.compareTo(s1)
    re.compile(r'Collections\.reverseOrder\(\)'),
    re.compile(r'Comparator\.reverseOrder\(\)'),
    re.compile(r'\.reversed\(\)'),  # stream().sorted(Comparator.reversed())
]

# Python sort without reverse=True
_PY_SORT_NO_REVERSE = re.compile(r'((?:sorted\s*\(|\.sort\s*\()(?:[^)]*?))(\s*\))', re.DOTALL)
_HAS_REVERSE = re.compile(r'reverse\s*=\s*True')


def _has_java_reverse(prompt: str) -> bool:
    return any(p.search(prompt) for p in _JAVA_REVERSE_PATTERNS)


def _has_python_reverse(completion: str) -> bool:
    return bool(_HAS_REVERSE.search(completion))


def _inject_reverse(completion: str) -> tuple[str, list[str]]:
    """Add reverse=True to the first sort() call missing it."""
    changes: list[str] = []

    def _replace(m: re.Match) -> str:
        inside = m.group(1)
        closing = m.group(2)
        if _HAS_REVERSE.search(inside):
            return m.group(0)
        # Add reverse=True before closing paren
        sep = ", " if inside.strip().rstrip("(") else ""
        changes.append("injected reverse=True in sort()")
        return inside + sep + "reverse=True" + closing

    fixed = _PY_SORT_NO_REVERSE.sub(_replace, completion, count=1)
    return fixed, changes


class ReverseSortFixer(Fixer):
    reason = "reverse_sort"

    def applies_to(self, record: dict[str, Any], stage: str) -> bool:
        lang_pair = str(record.get("lang_pair") or "")
        return stage == "pairs" and "java" in lang_pair.lower()

    def fix(self, record: dict[str, Any]) -> FixResult:
        prompt = record.get("prompt") or ""
        completion = record.get("completion") or ""
        if not prompt or not completion:
            return FixResult(record=record, fixed=False)
        if not _has_java_reverse(prompt):
            return FixResult(record=record, fixed=False)
        if _has_python_reverse(completion):
            return FixResult(record=record, fixed=False)
        fixed, changes = _inject_reverse(completion)
        if not changes:
            return FixResult(record=record, fixed=False)
        updated = {**record, "completion": fixed, "_reverse_sort_fixes": changes}
        return FixResult(record=updated, fixed=True, changes=changes)
