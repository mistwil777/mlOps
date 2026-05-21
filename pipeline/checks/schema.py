"""Schema validator — verifies that raw records match their collector's output_schema.

Architecture invariant enforced here:
  Raw records must NOT contain 'prompt' or 'completion' fields
  (those appear only at the transform step, step 8 of the pipeline).

Reads config/collectors/registry.yaml to know expected schemas.

CLI:
    python -m pipeline.checks.schema --input <file.jsonl> --collector multilang_wiki
    python -m pipeline.checks.schema --input <file.jsonl>   # auto-detects collector_type from records
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.checks.schema")

_REGISTRY_PATH = Path("config/collectors/registry.yaml")
_PREMATURE_FIELDS = {"prompt", "completion"}


def _load_registry() -> dict[str, Any]:
    with _REGISTRY_PATH.open() as fh:
        return yaml.safe_load(fh) or {}


def _get_expected_schema(collector_type: str) -> list[str]:
    reg = _load_registry()
    entry = reg.get("collectors", {}).get(collector_type, {})
    return entry.get("output_schema", [])


def check_record(
    record: dict[str, Any],
    collector_type: str | None = None,
) -> tuple[bool, str]:
    """Return (is_valid, reason).

    collector_type can be inferred from record fields if not provided.
    """
    # Reject records that carry prompt/completion at the raw stage
    premature = _PREMATURE_FIELDS.intersection(record.keys())
    # web_crawl is legacy — it already produces prompt/completion (output_schema: [])
    # Only block premature fields for collectors that have a defined output schema
    ct = collector_type or _infer_collector_type(record)
    if premature and ct not in ("web_crawl", "manual", None):
        return False, f"premature_fields ({', '.join(sorted(premature))}) — transform step only"

    schema = _get_expected_schema(ct) if ct else []
    if not schema:
        return True, "no_schema_defined"

    missing = [f for f in schema if f not in record]
    if missing:
        return False, f"missing_fields: {missing}"

    return True, "ok"


def _infer_collector_type(record: dict[str, Any]) -> str | None:
    if "task_name" in record and "lang" in record and "code" in record:
        return "multilang_wiki"
    if "description" in record and "code" in record:
        return "doc_crawl"
    if "prompt" in record and "completion" in record:
        return "web_crawl"
    return None


def filter_dataset(
    input_path: Path | str,
    output_path: Path | str | None = None,
    collector_type: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter JSONL file. Return (passed, rejected)."""
    records  = read_jsonl(input_path)
    passed:   list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for r in records:
        ok, reason = check_record(r, collector_type)
        if ok:
            passed.append(r)
        else:
            rejected.append({**r, "_schema_reject_reason": reason})
            logger.warning("Schema violation: %s", reason)

    logger.info(
        "Schema check: %d passed / %d rejected",
        len(passed), len(rejected),
    )

    if output_path and passed:
        write_jsonl(output_path, passed)

    return passed, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="Schema validation for raw JSONL records")
    parser.add_argument("--input",          required=True, help="JSONL file to validate")
    parser.add_argument("--output",         help="Output JSONL (valid records only)")
    parser.add_argument("--collector",      help="Collector type (auto-detected if omitted)")
    args = parser.parse_args()

    passed, rejected = filter_dataset(args.input, args.output, args.collector)
    if not args.output:
        print(f"Passed: {len(passed)}  Rejected: {len(rejected)}")
    for r in rejected:
        print(f"  REJECT: {r.get('_schema_reject_reason')}")


if __name__ == "__main__":
    main()
