"""Dynamic fixer loader — reads config/fixers/registry.yaml at runtime.

Usage:
    from pipeline.fixers import load_fixers

    # Load all enabled fixers for a given project context
    fixers = load_fixers(stage="pairs", domain="code", task_type="migration", lang_pair="java-python")
    for fixer in fixers:
        result = fixer.fix(record)
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

import yaml

from pipeline.fixers.base import Fixer

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path("config/fixers/registry.yaml")


def _load_registry() -> dict[str, Any]:
    if not _REGISTRY_PATH.exists():
        logger.warning("Fixer registry not found at %s — no fixers loaded", _REGISTRY_PATH)
        return {}
    with _REGISTRY_PATH.open() as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("fixers", {})


def _matches(spec_value: list | None, actual: str | None) -> bool:
    """Return True if actual matches any value in spec_value, or if spec_value is null (match all)."""
    if spec_value is None:
        return True
    if not actual:
        return False
    return actual in spec_value


def load_fixers(
    stage: str,
    domain: str | None = None,
    task_type: str | None = None,
    lang_pair: str | None = None,
) -> list[Fixer]:
    """Return enabled fixers matching the given pipeline context."""
    registry = _load_registry()
    fixers: list[Fixer] = []

    for name, spec in registry.items():
        if not spec.get("enabled", False):
            continue
        if spec.get("stage") != stage:
            continue
        if not _matches(spec.get("domains"), domain):
            continue
        if not _matches(spec.get("task_types"), task_type):
            continue
        if not _matches(spec.get("lang_pairs"), lang_pair):
            continue

        module_path = spec.get("module")
        class_name = spec.get("class_name")
        if not module_path or not class_name:
            logger.warning("Fixer '%s' missing module/class_name — skipped", name)
            continue

        try:
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            fixers.append(cls())
            logger.debug("Loaded fixer: %s (%s.%s)", name, module_path, class_name)
        except Exception as e:
            logger.error("Failed to load fixer '%s': %s", name, e)

    return fixers


def apply_fixers(
    records: list[dict[str, Any]],
    stage: str,
    domain: str | None = None,
    task_type: str | None = None,
    lang_pair: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Load and apply all matching fixers to a list of records.

    Returns (fixed_records, {reason: n_fixed}).
    """
    fixers = load_fixers(stage=stage, domain=domain, task_type=task_type, lang_pair=lang_pair)
    if not fixers:
        return records, {}

    stats: dict[str, int] = {}
    result = list(records)

    for fixer in fixers:
        n_fixed = 0
        for i, record in enumerate(result):
            if not fixer.applies_to(record, stage):
                continue
            fix_result = fixer.fix(record)
            if fix_result.fixed:
                result[i] = fix_result.record
                n_fixed += 1
        if n_fixed:
            stats[fixer.reason] = n_fixed
            logger.info("Fixer '%s': %d records fixed", fixer.reason, n_fixed)

    return result, stats
