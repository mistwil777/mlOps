"""Claude Code Stop hook: block the end of a turn when an architecture guard fails.

Standard library only (no pytest, no venv): runs on any machine with Python 3.
Runs every `test_*` function of the guard files below. Exit 2 sends the messages back to Claude
so it must fix the violation. Never blocks twice in a row (`stop_hook_active`).
"""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD_FILES = [
    ROOT / "tests" / "test_no_domain_hardcoding.py",
    ROOT / "tests" / "test_no_in_memory_job_state.py",
]


def _failures() -> list[str]:
    failures = []
    for file in GUARD_FILES:
        spec = importlib.util.spec_from_file_location(file.stem, file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name in sorted(n for n in dir(module) if n.startswith("test_")):
            try:
                getattr(module, name)()
            except AssertionError as exc:
                failures.append(f"[{file.name}::{name}] {exc}")
    return failures


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    if payload.get("stop_hook_active"):
        return 0

    failures = _failures()
    if failures:
        print("Architecture guard failed (CLAUDE.md rule 11):\n" + "\n".join(failures), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
