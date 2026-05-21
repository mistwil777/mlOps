"""LLM relevance scorer — rates how relevant a raw record is for an adapter's task.

Cost routing strategy:
  - Haiku for bulk scoring (cheap, fast)
  - Sonnet for records in the [threshold-0.1, threshold] grey zone (second opinion)

Prompt templates are adapter-specific, loaded from config/scoring/<adapter_id>.yaml if present.
Falls back to a generic template.

Requires: ANTHROPIC_API_KEY in environment.

CLI:
    python -m pipeline.checks.relevance --input <file.jsonl> --adapter <adapter_id> \\
        [--project <project_id>] [--output <out.jsonl>] [--threshold 0.7]
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.checks.relevance")

_SCORING_DIR   = Path("config/scoring")
_PROJECTS_DIR  = Path("config/projects")
_DEFAULT_THRESHOLD    = 0.7
_GREY_ZONE_DELTA      = 0.1   # haiku scores in [threshold - delta, threshold] → escalate to sonnet

_HAIKU_MODEL  = "claude-haiku-4-5-20251001"
_SONNET_MODEL = "claude-sonnet-4-6"

_GENERIC_PROMPT = """You are evaluating training data quality for a fine-tuning project.

Adapter: {adapter_name}
Task description: {task_description}

Record to evaluate:
{record_summary}

Rate the relevance of this record for training the adapter on a scale from 0 to 10.
Consider: technical correctness, specificity to the task, information density.
Reply with a single integer from 0 to 10, nothing else."""


def _load_scoring_config(adapter_id: str) -> dict[str, Any]:
    path = _SCORING_DIR / f"{adapter_id}.yaml"
    if path.exists():
        with path.open() as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _load_project_meta(project_id: str) -> dict[str, Any]:
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _record_summary(record: dict[str, Any], max_chars: int = 600) -> str:
    if "task_name" in record and "code" in record:
        name = str(record.get("task_name", ""))[:80]
        code = str(record.get("code", ""))[:max_chars]
        lang = str(record.get("lang", ""))
        return f"Task: {name}\nLanguage: {lang}\nCode:\n{code}"
    if "description" in record and "code" in record:
        desc = str(record.get("description", ""))[:200]
        code = str(record.get("code", ""))[:max_chars]
        return f"Description: {desc}\nCode:\n{code}"
    if "prompt" in record:
        p = str(record.get("prompt", ""))[:300]
        c = str(record.get("completion", ""))[:300]
        return f"Prompt: {p}\nCompletion: {c}"
    return str(record)[:max_chars]


def _call_llm(prompt: str, model: str) -> float:
    """Call Anthropic API and return a 0-1 score. Returns 0.5 on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — returning 0.5 for all records")
        return 0.5

    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=4,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip().split()[0]
        return float(raw) / 10.0
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM scoring failed (%s): %s", model, exc)
        return 0.5


def score_record(
    record: dict[str, Any],
    adapter_id: str,
    task_description: str = "",
    threshold: float = _DEFAULT_THRESHOLD,
) -> tuple[float, str]:
    """Score a single record. Return (score_0_to_1, model_used)."""
    scoring_cfg = _load_scoring_config(adapter_id)
    template    = scoring_cfg.get("prompt_template", _GENERIC_PROMPT)

    prompt = template.format(
        adapter_name=adapter_id,
        task_description=task_description or scoring_cfg.get("task_description", ""),
        record_summary=_record_summary(record),
    )

    # First pass with Haiku
    haiku_score = _call_llm(prompt, _HAIKU_MODEL)

    # Escalate to Sonnet for grey-zone records
    grey_min = threshold - _GREY_ZONE_DELTA
    if grey_min <= haiku_score < threshold:
        sonnet_score = _call_llm(prompt, _SONNET_MODEL)
        logger.debug(
            "Escalated to Sonnet: haiku=%.2f sonnet=%.2f",
            haiku_score, sonnet_score,
        )
        return sonnet_score, _SONNET_MODEL

    return haiku_score, _HAIKU_MODEL


def filter_dataset(
    input_path: Path | str,
    adapter_id: str,
    output_path: Path | str | None = None,
    rejected_path: Path | str | None = None,
    threshold: float = _DEFAULT_THRESHOLD,
    task_description: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter by relevance. Return (passed, rejected)."""
    records = read_jsonl(input_path)
    passed:   list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    cost_haiku  = 0
    cost_sonnet = 0

    for i, r in enumerate(records):
        if i % 50 == 0:
            logger.info(
                "Relevance scoring %d/%d (haiku=%d sonnet=%d)",
                i, len(records), cost_haiku, cost_sonnet,
            )
        score, model = score_record(r, adapter_id, task_description, threshold)
        tagged = {**r, "_relevance_score": round(score, 4), "_scored_by": model}

        if model == _HAIKU_MODEL:
            cost_haiku += 1
        else:
            cost_sonnet += 1

        if score >= threshold:
            passed.append(tagged)
        else:
            rejected.append({**tagged, "_relevance_reject_reason": f"score {score:.2f} < {threshold}"})

    logger.info(
        "Relevance filter (threshold=%.2f): %d passed / %d rejected — haiku=%d sonnet=%d",
        threshold, len(passed), len(rejected), cost_haiku, cost_sonnet,
    )

    if output_path and passed:
        write_jsonl(output_path, passed)
    if rejected_path and rejected:
        write_jsonl(rejected_path, rejected)

    return passed, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM relevance scorer for raw JSONL records")
    parser.add_argument("--input",       required=True)
    parser.add_argument("--adapter",     required=True, help="Adapter ID (e.g. syntax_translator)")
    parser.add_argument("--project",     help="Project ID (optional — used to load task_description)")
    parser.add_argument("--output",      help="Output JSONL (passed records only)")
    parser.add_argument("--rejected",    help="Output JSONL for rejected records")
    parser.add_argument("--threshold",   type=float, default=_DEFAULT_THRESHOLD)
    args = parser.parse_args()

    task_desc = ""
    if args.project:
        meta = _load_project_meta(args.project)
        task_desc = meta.get("description", "")

    passed, rejected = filter_dataset(
        args.input, args.adapter, args.output, args.rejected, args.threshold, task_desc,
    )
    if not args.output:
        print(f"Passed: {len(passed)}  Rejected: {len(rejected)}")


if __name__ == "__main__":
    main()
