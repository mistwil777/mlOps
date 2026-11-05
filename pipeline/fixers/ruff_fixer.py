"""Fixer: ruff check → fix → format on Python completions."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pipeline.fixers.base import FixResult, Fixer

logger = logging.getLogger(__name__)

_PYTHON_LANGS = {"python", "py", ""}


def _strip_markdown_fences(code: str) -> str:
    code = re.sub(r"^```[a-zA-Z]*\n?", "", code, flags=re.MULTILINE)
    code = re.sub(r"^```\s*$", "", code, flags=re.MULTILINE)
    return code.strip()


def _ruff_available() -> bool:
    try:
        subprocess.run(["ruff", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def ruff_fix_completion(completion: str) -> tuple[str, bool, str | None]:
    """Run ruff on a single Python completion.

    Returns (fixed_completion, was_changed, reject_reason_or_None).
    reject_reason is set when E9xx syntax errors remain after fix.
    """
    clean = _strip_markdown_fences(completion)
    had_fences = clean != completion.strip()

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
        f.write(clean)
        tmp = f.name

    try:
        subprocess.run(["ruff", "check", tmp], capture_output=True, text=True)
        subprocess.run(["ruff", "check", "--fix", "--unsafe-fixes=false", tmp], capture_output=True, text=True)
        subprocess.run(["ruff", "format", tmp], capture_output=True, text=True)

        fixed_code = Path(tmp).read_text(encoding="utf-8")

        recheck = subprocess.run(["ruff", "check", tmp], capture_output=True, text=True)
        syntax_errors = [l for l in recheck.stdout.splitlines() if re.search(r"\bE9\d\d\b", l)]
        if syntax_errors:
            return completion, False, f"ruff_syntax: {syntax_errors[0]}"

        changed = fixed_code.strip() != clean.strip()
        if changed:
            final = f"```python\n{fixed_code.strip()}\n```" if had_fences else fixed_code.strip()
            return final, True, None
        return completion, False, None
    finally:
        os.unlink(tmp)


class RuffFixer(Fixer):
    reason = "syntax_error"
    _checked_available: bool | None = None

    def _is_available(self) -> bool:
        if RuffFixer._checked_available is None:
            RuffFixer._checked_available = _ruff_available()
            if not RuffFixer._checked_available:
                logger.warning("ruff not found — RuffFixer will be a no-op (pip install ruff)")
        return RuffFixer._checked_available

    def applies_to(self, record: dict[str, Any], stage: str) -> bool:
        lang = str(record.get("lang") or record.get("lang_pair") or "").lower()
        tgt = lang.split("-")[-1] if "-" in lang else lang
        return stage == "pairs" and tgt in _PYTHON_LANGS

    def fix(self, record: dict[str, Any]) -> FixResult:
        if not self._is_available():
            return FixResult(record=record, fixed=False)
        completion = record.get("completion") or ""
        if not completion.strip():
            return FixResult(record=record, fixed=False)
        fixed, changed, reject_reason = ruff_fix_completion(completion)
        if reject_reason:
            updated = {**record, "reject_reason": reject_reason, "_ruff_rejected": True}
            return FixResult(record=updated, fixed=False, changes=[reject_reason])
        if changed:
            updated = {**record, "completion": fixed, "_ruff_fixed": True}
            return FixResult(record=updated, fixed=True, changes=["ruff auto-fix applied"])
        return FixResult(record=record, fixed=False)
