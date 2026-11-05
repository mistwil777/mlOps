"""Base interface for all deterministic data-quality fixers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FixResult:
    record: dict[str, Any]
    fixed: bool
    changes: list[str] = field(default_factory=list)


class Fixer(ABC):
    """Deterministic fixer applied to a record before LLM scoring.

    Subclasses implement fix() for a single record.
    The fixer must be idempotent: applying it twice must produce the same result.
    """

    reason: str  # matches defect_clusters.reason

    @abstractmethod
    def fix(self, record: dict[str, Any]) -> FixResult:
        """Apply the fix to a single record. Must not raise — return fixed=False on skip."""
        ...

    def applies_to(self, record: dict[str, Any], stage: str) -> bool:
        """Return True if this fixer should run on this record at this pipeline stage."""
        return True

    def fix_batch(self, records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Apply fix() to a list of records. Returns (records, n_fixed)."""
        n_fixed = 0
        for r in records:
            result = self.fix(r)
            if result.fixed:
                records[records.index(r)] = result.record
                n_fixed += 1
        return records, n_fixed
