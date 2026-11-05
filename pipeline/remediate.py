"""Standalone remédiation — applique les fixers du registry sur train.jsonl / valid.jsonl existants.

Usage:
    python -m pipeline.remediate --project migrator
    python -m pipeline.remediate --project migrator --dry-run
    python -m pipeline.remediate --project migrator --stage raw  # remédiation sur processed/clean_raw/

Ce script est déclenché automatiquement quand un defect_cluster dépasse le seuil,
et peut aussi être lancé manuellement après une session d'audit.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from pipeline.fixers import apply_fixers, load_fixers
from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.remediate")


def _load_project_config(project: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open() as fh:
        return yaml.safe_load(fh)


def _project_context(config: dict[str, Any]) -> tuple[str, str | None, str | None]:
    domain = config.get("domain", "code")
    task_type = config.get("task_type")
    lang_pair = None
    adapters = config.get("adapters", [])
    if adapters:
        lang_pair = adapters[0].get("lang_pair")
    return domain, task_type, lang_pair


def remediate_file(
    path: Path,
    stage: str,
    domain: str,
    task_type: str | None,
    lang_pair: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply all matching fixers to a JSONL file. Returns a report dict."""
    if not path.exists():
        logger.warning("File not found: %s — skipped", path)
        return {"file": str(path), "skipped": True, "reason": "not_found"}

    records = read_jsonl(path)
    n_before = len(records)

    fixed, stats = apply_fixers(records, stage=stage, domain=domain, task_type=task_type, lang_pair=lang_pair)

    total_fixed = sum(stats.values())
    report = {
        "file": str(path),
        "stage": stage,
        "n_records": n_before,
        "n_fixed": total_fixed,
        "by_fixer": stats,
        "dry_run": dry_run,
    }

    if total_fixed > 0 and not dry_run:
        write_jsonl(path, fixed)
        logger.info("Remédiation %s: %d/%d records fixed → written", path.name, total_fixed, n_before)
    elif total_fixed > 0:
        logger.info("[dry-run] Remédiation %s: would fix %d/%d records", path.name, total_fixed, n_before)
    else:
        logger.info("Remédiation %s: nothing to fix", path.name)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply deterministic fixers to existing pipeline data")
    parser.add_argument("--project", required=True)
    parser.add_argument("--stage", choices=["raw", "pairs", "all"], default="all",
                        help="Pipeline stage to remediate (default: all)")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Report changes without writing files")
    parser.add_argument("--report", default=None, help="Write JSON report to this path")
    args = parser.parse_args()

    config = _load_project_config(args.project)
    domain, task_type, lang_pair = _project_context(config)

    base = Path(f"datalake/projects/{args.project}")
    reports: list[dict[str, Any]] = []

    # List available fixers for this project
    all_stages = ["raw", "pairs"] if args.stage == "all" else [args.stage]
    for stage in all_stages:
        fixers = load_fixers(stage=stage, domain=domain, task_type=task_type, lang_pair=lang_pair)
        if not fixers:
            logger.info("No enabled fixers for stage=%s domain=%s — skipped", stage, domain)
            continue
        logger.info("Stage=%s: %d fixers loaded (%s)", stage, len(fixers), [f.reason for f in fixers])

    # Stage: raw — apply on processed/clean_raw/
    if args.stage in ("raw", "all"):
        clean_raw_dir = base / "processed" / "clean_raw"
        for jsonl_file in sorted(clean_raw_dir.glob("*.jsonl")):
            r = remediate_file(jsonl_file, stage="raw", domain=domain,
                               task_type=task_type, lang_pair=lang_pair, dry_run=args.dry_run)
            reports.append(r)

    # Stage: pairs — apply on fine-tuning-ready/train.jsonl + valid.jsonl
    if args.stage in ("pairs", "all"):
        ftr_dir = base / "fine-tuning-ready"
        for fname in ("train.jsonl", "valid.jsonl"):
            fpath = ftr_dir / fname
            r = remediate_file(fpath, stage="pairs", domain=domain,
                               task_type=task_type, lang_pair=lang_pair, dry_run=args.dry_run)
            reports.append(r)

    # Summary
    total_fixed = sum(r.get("n_fixed", 0) for r in reports)
    total_records = sum(r.get("n_records", 0) for r in reports if not r.get("skipped"))
    print(f"\nRemédiation — projet: {args.project}")
    print(f"  Records processed : {total_records}")
    print(f"  Records fixed     : {total_fixed}")
    if args.dry_run:
        print("  [dry-run] — no files written")
    for r in reports:
        if r.get("skipped"):
            continue
        if r["n_fixed"]:
            print(f"  {Path(r['file']).name}: {r['n_fixed']} fixed — {r['by_fixer']}")

    if args.report:
        Path(args.report).write_text(json.dumps(reports, indent=2, ensure_ascii=False))
        print(f"\nReport → {args.report}")


if __name__ == "__main__":
    main()
