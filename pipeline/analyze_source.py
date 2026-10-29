"""Semantic analysis of ingested raw documents.

Runs BEFORE transform. Samples raw documents, calls an LLM to propose a
category taxonomy, compares it against the project config, and saves a
validation report. transform.py should not run until the report is validated.

Usage:
    python -m pipeline.analyze_source --project <project_id>
    python -m pipeline.analyze_source --project p01-tests-unitaires --sample 30
"""

from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.analyze_source")

_PROJECTS_DIR = Path("config/projects")
_SAMPLE_SIZE_DEFAULT = 20
_MAX_CHARS_PER_DOC = 1200


def _load_project(project_id: str) -> dict[str, Any]:
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _sample_raw_docs(raw_dir: Path, n: int) -> list[dict[str, str]]:
    """Return up to n documents from raw dir as {filename, content} dicts."""
    files = list(raw_dir.rglob("*"))
    files = [f for f in files if f.is_file() and f.suffix in {".py", ".java", ".ts", ".js", ".txt", ".md", ".json", ".jsonl"}]
    if not files:
        raise ValueError(f"No readable files found in {raw_dir}")

    sample = random.sample(files, min(n, len(files)))
    docs = []
    for f in sample:
        try:
            content = f.read_text(errors="replace")[:_MAX_CHARS_PER_DOC]
            docs.append({"filename": f.name, "content": content})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read %s: %s", f, exc)
    return docs


def _build_prompt(project: dict[str, Any], docs: list[dict[str, str]]) -> str:
    existing_categories = project.get("categories", [])
    existing_names = [c.get("name", "") for c in existing_categories] if existing_categories else []

    docs_block = "\n\n".join(
        f"--- {d['filename']} ---\n{d['content']}" for d in docs
    )

    existing_block = (
        f"Categories already declared in config: {existing_names}"
        if existing_names
        else "No categories declared yet in config."
    )

    return f"""You are analyzing raw source documents for an MLOps fine-tuning project.

Project: {project.get('name', project.get('id', ''))}
Domain: {project.get('domain', 'unknown')}
Task type: {project.get('task_type', 'unknown')}
{existing_block}

Below are {len(docs)} sampled documents from the raw datalake.

{docs_block}

---

Your task:
1. Identify the distinct CATEGORIES of content present in these documents.
   A category is a semantically distinct type of input/output pattern that would
   require dedicated training pairs (e.g. "unit_test_simple", "mock_setup", "parameterized_test").
2. For each category: estimate how many documents belong to it (out of the sample).
3. List categories that seem present but are NOT in the existing config (missing_from_config).
4. For each missing category, explain briefly why it matters for training.

Respond ONLY with valid JSON, no markdown, no explanation outside JSON:
{{
  "proposed_categories": [
    {{
      "name": "snake_case_name",
      "description": "one sentence",
      "estimated_count_in_sample": 5,
      "example_filename": "filename.java"
    }}
  ],
  "missing_from_config": [
    {{
      "name": "snake_case_name",
      "reason": "why this category matters for training quality"
    }}
  ],
  "analyst_notes": "optional overall observations"
}}"""


def _parse_llm_response(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("LLM response contained no JSON object")
    return json.loads(match.group())


def analyze(project_id: str, sample_size: int = _SAMPLE_SIZE_DEFAULT) -> Path:
    """Run analysis and return path to saved report."""
    project = _load_project(project_id)
    raw_dir = Path(project["data"]["raw"])

    if not raw_dir.exists():
        raise FileNotFoundError(f"Raw data directory not found: {raw_dir}")

    logger.info("Sampling %d documents from %s", sample_size, raw_dir)
    docs = _sample_raw_docs(raw_dir, sample_size)
    logger.info("Sampled %d documents", len(docs))

    prompt = _build_prompt(project, docs)
    logger.info("Calling LLM for category analysis…")
    raw_response = call_llm(prompt, max_tokens=2048, temperature=0.2)

    if not raw_response:
        raise RuntimeError("LLM returned no response")

    try:
        analysis = _parse_llm_response(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("Could not parse LLM response: %s", exc)
        logger.debug("Raw response: %s", raw_response)
        raise

    report: dict[str, Any] = {
        "project_id": project_id,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(docs),
        "total_files_in_raw": len(list(raw_dir.rglob("*"))),
        "proposed_categories": analysis.get("proposed_categories", []),
        "missing_from_config": analysis.get("missing_from_config", []),
        "analyst_notes": analysis.get("analyst_notes", ""),
        "validation_status": "pending",
        "validated_by": None,
        "validated_at": None,
    }

    report_path = raw_dir.parent / "analysis_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    logger.info("Report saved to %s", report_path)

    _print_summary(report)
    return report_path


def _print_summary(report: dict[str, Any]) -> None:
    print(f"\n{'='*60}")
    print(f"Source Analysis — {report['project_id']}")
    print(f"Sample: {report['sample_size']} / {report['total_files_in_raw']} files")
    print(f"{'='*60}")
    print(f"\nProposed categories ({len(report['proposed_categories'])}):")
    for cat in report["proposed_categories"]:
        print(f"  • {cat['name']} — {cat['estimated_count_in_sample']} occurrences in sample")
        print(f"    {cat['description']}")
    if report["missing_from_config"]:
        print(f"\n⚠  Missing from config ({len(report['missing_from_config'])}):")
        for m in report["missing_from_config"]:
            print(f"  • {m['name']}: {m['reason']}")
    if report["analyst_notes"]:
        print(f"\nNotes: {report['analyst_notes']}")
    print(f"\nStatus: {report['validation_status'].upper()}")
    print(f"Report: {report['project_id']}/analysis_report.json\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze raw source documents for a project")
    parser.add_argument("--project", required=True, help="Project ID (matches config/projects/<id>.yaml)")
    parser.add_argument("--sample", type=int, default=_SAMPLE_SIZE_DEFAULT, help="Number of documents to sample")
    args = parser.parse_args()

    analyze(args.project, args.sample)
