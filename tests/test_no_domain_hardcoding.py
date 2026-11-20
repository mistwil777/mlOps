"""Guard: executable code must stay domain-agnostic (rule 11 in CLAUDE.md).

Ratchet: tests/domain_hardcoding_baseline.json lists the existing debt per file (occurrence counts).
The test fails when a file gets MORE occurrences than its baseline, or a new file has any.
The baseline may only shrink: `python tests/test_no_domain_hardcoding.py --shrink` lowers it, never raises it.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).with_name("domain_hardcoding_baseline.json")
DIRS = ("pipeline", "agents", "api", "scripts")
FORBIDDEN = re.compile(r"\b(migrator|javac|codebleu|java)\b", re.IGNORECASE)


def _count_occurrences() -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in DIRS:
        for path in (ROOT / d).rglob("*.py"):
            n = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if FORBIDDEN.search(line))
            if n:
                counts[path.relative_to(ROOT).as_posix()] = n
    return counts


def _baseline() -> dict[str, int]:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def test_no_new_domain_hardcoding() -> None:
    base = _baseline()
    worse = [
        f"{rel}: {n} occurrence(s), baseline {base.get(rel, 0)}"
        for rel, n in sorted(_count_occurrences().items())
        if n > base.get(rel, 0)
    ]
    assert not worse, "New hardcoded domain reference(s) - use the project config instead:\n" + "\n".join(worse)


def shrink() -> int:
    base, current = _baseline(), _count_occurrences()
    first_run = not BASELINE.exists()
    if not first_run and any(n > base.get(rel, 0) for rel, n in current.items()):
        print("Refusing to raise the baseline: fix the new occurrences first.")
        return 1
    BASELINE.write_text(json.dumps(dict(sorted(current.items())), indent=2) + "\n", encoding="utf-8")
    print(f"Baseline: {sum(current.values())} occurrence(s) in {len(current)} file(s)")
    return 0


if __name__ == "__main__":
    if "--shrink" not in sys.argv:
        sys.exit("usage: python tests/test_no_domain_hardcoding.py --shrink")
    sys.exit(shrink())
