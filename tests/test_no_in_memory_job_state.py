"""Guard: job state lives in the durable ledger (pipeline/utils/run_ledger.py), never in a module-level dict."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IN_MEMORY_JOBS = re.compile(r"^_?[A-Z_]*JOBS\b[^=\n]*=\s*\{", re.MULTILINE)


def test_no_in_memory_job_state() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "api").rglob("*.py")
        if IN_MEMORY_JOBS.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "In-memory job dict(s) found - use get_ledger() from pipeline.utils.run_ledger instead:\n"
        + "\n".join(offenders)
    )
