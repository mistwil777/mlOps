"""Data quality pipeline — orchestrates all verifier checks in order.

Pipeline order (deterministic first, LLM last):
  1. schema    — required fields present, no premature prompt/completion
  2. syntax    — code parses without errors (tree-sitter / heuristic)
  3. text      — length, entropy, boilerplate
  4. dedup     — exact hash + near-duplicate embedding
  5. relevance — LLM scoring (Haiku, escalates to Sonnet for grey zone)

Each step filters records. Rejected records are collected per-step for audit.
Enabled steps are read from config/verifiers/registry.yaml.

CLI:
    python -m pipeline.qualify --project <id> [--adapter <adapter_id>]
    python -m pipeline.qualify --input <file.jsonl> --output <out.jsonl> --adapter <id>
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.qualify")

_REGISTRY_PATH = Path("config/verifiers/registry.yaml")
_PROJECTS_DIR  = Path("config/projects")


def _load_registry() -> dict[str, Any]:
    with _REGISTRY_PATH.open() as fh:
        return yaml.safe_load(fh) or {}


def _is_enabled(registry: dict[str, Any], key: str) -> bool:
    return registry.get("verifiers", {}).get(key, {}).get("enabled", False)


def qualify(
    records: list[dict[str, Any]],
    adapter_id: str,
    project_id: str | None = None,
    threshold_relevance: float = 0.7,
    threshold_near_dup:  float = 0.97,
) -> dict[str, Any]:
    """Run the full quality pipeline on a list of records.

    Returns a dict with:
      passed    : list of records that passed all enabled checks
      rejected  : dict[step_name → list of rejected records]
      stats     : dict[step_name → {input, passed, rejected}]
    """
    from pipeline.checks.schema      import check_record as schema_check
    from pipeline.checks.syntax      import check_record as syntax_check
    from pipeline.checks.text_quality import check_record as text_check
    from pipeline.checks.similarity  import find_near_duplicates
    from pipeline.checks.relevance   import score_record

    reg = _load_registry()

    task_description = ""
    if project_id:
        path = _PROJECTS_DIR / f"{project_id}.yaml"
        if path.exists():
            with path.open() as fh:
                task_description = (yaml.safe_load(fh) or {}).get("description", "")

    rejected: dict[str, list[dict[str, Any]]] = {
        "schema": [], "syntax": [], "text_quality": [], "dedup": [], "relevance": [],
    }
    stats: dict[str, dict[str, int]] = {}
    current = list(records)

    # Step 1 — Schema
    if _is_enabled(reg, "schema_validation"):
        before = len(current)
        ok, fail = [], []
        for r in current:
            valid, reason = schema_check(r)
            (ok if valid else fail).append({**r, "_schema_reason": reason} if not valid else r)
        rejected["schema"] = fail
        current = ok
        stats["schema"] = {"input": before, "passed": len(ok), "rejected": len(fail)}
        logger.info("Schema: %d → %d (-%d)", before, len(ok), len(fail))

    # Step 2 — Syntax
    if _is_enabled(reg, "code_syntax"):
        before = len(current)
        ok, fail = [], []
        for r in current:
            valid, reason = syntax_check(r)
            (ok if valid else fail).append({**r, "_syntax_reason": reason} if not valid else r)
        rejected["syntax"] = fail
        current = ok
        stats["syntax"] = {"input": before, "passed": len(ok), "rejected": len(fail)}
        logger.info("Syntax: %d → %d (-%d)", before, len(ok), len(fail))

    # Step 3 — Text quality
    if _is_enabled(reg, "text_quality"):
        before = len(current)
        ok, fail = [], []
        for r in current:
            valid, reason = text_check(r)
            (ok if valid else fail).append({**r, "_text_reason": reason} if not valid else r)
        rejected["text_quality"] = fail
        current = ok
        stats["text_quality"] = {"input": before, "passed": len(ok), "rejected": len(fail)}
        logger.info("Text quality: %d → %d (-%d)", before, len(ok), len(fail))

    # Step 4 — Deduplication (exact + near-dup)
    if _is_enabled(reg, "deduplication"):
        before = len(current)
        dup_params = reg.get("verifiers", {}).get("deduplication", {}).get("params", {})
        field      = dup_params.get("exact_field", "code")

        # Exact hash dedup
        import hashlib, json  # noqa: PLC0415, E401
        seen: set[str] = set()
        after_exact: list[dict[str, Any]] = []
        for r in current:
            raw = r.get(field)
            key = hashlib.sha256(
                (" ".join(str(raw).lower().split()) if raw else json.dumps(r, sort_keys=True)).encode()
            ).hexdigest()
            if key not in seen:
                seen.add(key)
                after_exact.append(r)
            else:
                rejected["dedup"].append({**r, "_dedup_reason": "exact_hash_duplicate"})

        # Near-dup
        dup_pairs = find_near_duplicates(after_exact, field, threshold_near_dup)
        dup_idx   = {b for _, b, _ in dup_pairs}
        after_near = [r for i, r in enumerate(after_exact) if i not in dup_idx]
        for i in dup_idx:
            rejected["dedup"].append({**after_exact[i], "_dedup_reason": "near_duplicate"})

        current = after_near
        stats["dedup"] = {"input": before, "passed": len(current), "rejected": before - len(current)}
        logger.info("Dedup: %d → %d (-%d)", before, len(current), before - len(current))

    # Step 5 — Relevance (LLM, most expensive — runs last)
    if _is_enabled(reg, "semantic_relevance"):
        before = len(current)
        ok, fail = [], []
        for r in current:
            score, model = score_record(r, adapter_id, task_description, threshold_relevance)
            tagged = {**r, "_relevance_score": round(score, 4), "_scored_by": model}
            if score >= threshold_relevance:
                ok.append(tagged)
            else:
                fail.append({**tagged, "_relevance_reason": f"score {score:.2f}"})
        rejected["relevance"] = fail
        current = ok
        stats["relevance"] = {"input": before, "passed": len(ok), "rejected": len(fail)}
        logger.info("Relevance: %d → %d (-%d)", before, len(ok), len(fail))

    return {"passed": current, "rejected": rejected, "stats": stats}


def run_for_project(project_id: str, adapter_id: str | None = None) -> None:
    """Run quality pipeline for a project's consolidated raw file."""
    base = Path(f"datalake/projects/{project_id}/raw")
    input_path  = base / "consolidated.jsonl"
    output_base = Path(f"datalake/projects/{project_id}/processed")
    output_base.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        raise FileNotFoundError(f"{input_path} not found. Collect data first.")

    records = read_jsonl(input_path)
    logger.info("Qualifying %d records for project %s", len(records), project_id)

    result = qualify(
        records,
        adapter_id=adapter_id or project_id,
        project_id=project_id,
    )

    write_jsonl(output_base / "qualified.jsonl", result["passed"])

    for step, recs in result["rejected"].items():
        if recs:
            write_jsonl(output_base / f"rejected_{step}.jsonl", recs)

    logger.info(
        "Quality pipeline complete: %d qualified records written to %s",
        len(result["passed"]), output_base / "qualified.jsonl",
    )

    # Print summary table
    print(f"\n{'Step':<16} {'Input':>6} {'Passed':>6} {'Rejected':>8}")
    print("-" * 40)
    for step, s in result["stats"].items():
        print(f"{step:<16} {s['input']:>6} {s['passed']:>6} {s['rejected']:>8}")
    print(f"\nFinal qualified records: {len(result['passed'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Data quality pipeline")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project ID")
    group.add_argument("--input",   help="Input JSONL file")
    parser.add_argument("--output",    help="Output JSONL (required with --input)")
    parser.add_argument("--adapter",   help="Adapter ID for relevance scoring")
    parser.add_argument("--threshold", type=float, default=0.7, help="Relevance threshold (default 0.7)")
    args = parser.parse_args()

    if args.project:
        run_for_project(args.project, args.adapter)
    else:
        if not args.output:
            parser.error("--output required with --input")
        records = read_jsonl(args.input)
        if not args.adapter:
            parser.error("--adapter required with --input")
        result = qualify(records, args.adapter, threshold_relevance=args.threshold)
        write_jsonl(args.output, result["passed"])
        print(f"Qualified: {len(result['passed'])} / {len(records)}")


if __name__ == "__main__":
    main()
