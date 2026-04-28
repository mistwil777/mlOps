"""Deploy gate — verifies MLflow eval metrics meet project thresholds before promotion."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import mlflow
import yaml


def _load_thresholds(project: str) -> dict[str, float]:
    path = Path("config/projects") / f"{project}.yaml"
    cfg = yaml.safe_load(path.read_text())
    return cfg.get("quality_thresholds", {})


def _load_domain_thresholds(project: str) -> dict[str, float]:
    """Load domain-specific eval thresholds if an eval_domain is configured."""
    path = Path("config/projects") / f"{project}.yaml"
    cfg = yaml.safe_load(path.read_text())
    domain = cfg.get("eval_domain")
    if not domain:
        return {}
    domain_path = Path("evals/domains") / f"{domain}.yaml"
    if not domain_path.exists():
        return {}
    domain_cfg = yaml.safe_load(domain_path.read_text())
    return domain_cfg.get("gate_thresholds", {})


def _check_mlflow_run(mlflow_run_id: str, thresholds: dict[str, float]) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failures) for a given MLflow run."""
    client = mlflow.tracking.MlflowClient()
    run = client.get_run(mlflow_run_id)

    if run.info.status != "FINISHED":
        return False, [f"run status is '{run.info.status}', expected FINISHED"]

    metrics = run.data.metrics
    failures: list[str] = []

    threshold_map: dict[str, tuple[str, float]] = {
        # (comparison, value) — "gte" means metric >= threshold, "lte" means metric <= threshold
        "min_pass_rate":       ("gte", thresholds.get("min_pass_rate", 0.0)),
        "min_rouge_l":         ("gte", thresholds.get("min_rouge_l", 0.0)),
        "min_codebleu":        ("gte", thresholds.get("min_codebleu", 0.0)),
        "min_compilation_rate":("gte", thresholds.get("min_compilation_rate", 0.0)),
        "min_ner_f1":          ("gte", thresholds.get("min_ner_f1", 0.0)),
        "max_latency_ratio":   ("lte", thresholds.get("max_latency_ratio", 999.0)),
        "max_fpr":             ("lte", thresholds.get("max_fpr", 1.0)),
    }

    for threshold_key, (cmp, threshold_val) in threshold_map.items():
        if threshold_val in (0.0, 999.0, 1.0):
            continue  # threshold not configured — skip
        # Map threshold key to likely MLflow metric name
        metric_key = threshold_key.removeprefix("min_").removeprefix("max_")
        if metric_key not in metrics:
            failures.append(f"metric '{metric_key}' missing from MLflow run")
            continue
        actual = metrics[metric_key]
        if cmp == "gte" and actual < threshold_val:
            failures.append(f"{metric_key}={actual:.4f} < required {threshold_val:.4f}")
        elif cmp == "lte" and actual > threshold_val:
            failures.append(f"{metric_key}={actual:.4f} > required {threshold_val:.4f}")

    return len(failures) == 0, failures


def run_gate(project: str, mlflow_run_id: str, output_json: str | None = None) -> bool:
    """Main gate logic. Returns True if all checks pass."""
    print(f"Deploy gate: project={project} run={mlflow_run_id}")

    thresholds = _load_thresholds(project)
    domain_thresholds = _load_domain_thresholds(project)
    merged = {**thresholds, **domain_thresholds}  # domain overrides generic

    if not merged:
        print("  WARNING: no quality_thresholds configured — gate trivially passes")

    passed, failures = _check_mlflow_run(mlflow_run_id, merged)

    result: dict[str, Any] = {
        "project": project,
        "mlflow_run_id": mlflow_run_id,
        "passed": passed,
        "failures": failures,
        "thresholds_checked": merged,
    }

    if output_json:
        Path(output_json).write_text(json.dumps(result, indent=2))

    if passed:
        print("  GATE PASSED — model eligible for promotion to production")
    else:
        print("  GATE FAILED:")
        for f in failures:
            print(f"    ✗ {f}")

    return passed


def promote(project: str, model_id: str) -> None:
    """Move a fused model from staging to production (atomic rename)."""
    staging = Path("models/staging") / project / model_id
    production = Path("models/production") / project / model_id

    if not staging.exists():
        print(f"ERROR: staging path not found: {staging}")
        sys.exit(1)

    production.parent.mkdir(parents=True, exist_ok=True)
    staging.rename(production)
    print(f"Promoted: {staging} → {production}")

    # Write pointer file for quick lookup
    pointer = Path("models/production") / project / "current"
    pointer.write_text(str(production))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="MLOps deploy gate")
    sub = parser.add_subparsers(dest="cmd")

    g = sub.add_parser("check", help="Check gate for a given MLflow run")
    g.add_argument("--project", required=True)
    g.add_argument("--run-id", required=True)
    g.add_argument("--output", help="Write JSON result to file")

    p = sub.add_parser("promote", help="Move model staging → production")
    p.add_argument("--project", required=True)
    p.add_argument("--model-id", required=True)

    args = parser.parse_args()

    if args.cmd == "check":
        ok = run_gate(args.project, args.run_id, args.output)
        sys.exit(0 if ok else 1)
    elif args.cmd == "promote":
        promote(args.project, args.model_id)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
