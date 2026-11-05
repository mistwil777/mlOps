"""Fixer: inject missing standard imports into Python completions."""

from __future__ import annotations

import re
from typing import Any

from pipeline.fixers.base import FixResult, Fixer

# (usage_pattern, import_presence_checks, import_line_to_add)
# import_presence_checks: list of strings — if ANY is found in non-comment import lines → skip
_IMPORT_FIXES: list[tuple[str, list[str], str]] = [
    (r"\bnp\.", ["import numpy"], "import numpy as np"),
    (r"\bpd\.", ["import pandas"], "import pandas as pd"),
    (r"\bpytest\.", ["import pytest"], "import pytest"),
    (r"\bos\.", ["import os"], "import os"),
    (r"\bsys\.", ["import sys"], "import sys"),
    (r"\bre\.", ["import re"], "import re"),
    (r"\bjson\.", ["import json"], "import json"),
    (r"\bdatetime\.", ["import datetime", "from datetime"], "from datetime import datetime"),
    # Path( can be satisfied by either "from pathlib import Path" OR "import pathlib"
    (r"\bPath\(", ["from pathlib", "import pathlib"], "from pathlib import Path"),
    (r"@dataclass", ["from dataclasses"], "from dataclasses import dataclass"),
    (
        r"\bTyping\b|\bList\[|\bDict\[|\bOptional\[|\bTuple\[",
        ["from typing"],
        "from typing import List, Dict, Optional, Tuple, Any",
    ),
]


def _existing_imports(completion: str) -> str:
    """Return only non-comment import lines as a single string for presence checks."""
    lines = []
    for line in completion.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(("import ", "from ")):
            lines.append(stripped)
    return "\n".join(lines)


def fix_missing_imports(completion: str) -> tuple[str, list[str]]:
    """Add missing standard imports to a Python completion. Returns (fixed_completion, added)."""
    if not completion or not completion.strip():
        return completion, []

    existing = _existing_imports(completion)
    lines = completion.split("\n")
    imports_to_add: list[str] = []
    added: list[str] = []

    for pattern, checks, import_line in _IMPORT_FIXES:
        if not re.search(pattern, completion):
            continue
        if any(check in existing for check in checks):
            continue
        imports_to_add.append(import_line)
        added.append(import_line)

    if not imports_to_add:
        return completion, []

    insert_pos = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            insert_pos = i + 1
        elif insert_pos > 0:
            break

    for imp in reversed(imports_to_add):
        lines.insert(insert_pos, imp)

    return "\n".join(lines), added


class MissingImportFixer(Fixer):
    reason = "missing_import"

    def fix(self, record: dict[str, Any]) -> FixResult:
        completion = record.get("completion") or ""
        if not completion:
            return FixResult(record=record, fixed=False)
        fixed, added = fix_missing_imports(completion)
        if not added:
            return FixResult(record=record, fixed=False)
        updated = {**record, "completion": fixed, "_import_fixes_applied": added}
        return FixResult(record=updated, fixed=True, changes=added)
