"""Generate training pairs for a project using Nova Pro + LLM-as-judge.

Usage:
    python scripts/generate_pairs.py --project p01-tests-unitaires [--n 100] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.llm import call_llm_with_config
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.generate_pairs")

# ── Config loading ──────────────────────────────────────────────────────────

def load_project_config(project: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project}.yaml"
    with open(path) as fh:
        return yaml.safe_load(fh)


def load_meta_prompt(project: str) -> str:
    path = Path("datalake/projects") / project / "meta-prompt.md"
    return path.read_text(encoding="utf-8")


def load_judge_rubric(project: str) -> dict[str, Any]:
    path = Path("datalake/projects") / project / "judge-rubric.yaml"
    with open(path) as fh:
        return yaml.safe_load(fh)


# ── Generation ──────────────────────────────────────────────────────────────

CATEGORIES = {
    "A": {"label": "Opérations simples",      "n": 25},
    "B": {"label": "Transactions financières", "n": 30},
    "C": {"label": "Validation et erreurs",    "n": 25},
    "D": {"label": "Services multi-mocks",     "n": 20},
}


def build_generation_prompt(meta_prompt: str, category: str, n_pairs: int) -> str:
    prompt = meta_prompt.replace("{n_pairs}", str(n_pairs)).replace("{category}", category)
    return prompt


def extract_json_array(text: str) -> list[dict]:
    """Extract first JSON array from LLM response.

    Handles nested code blocks inside completion strings by locating the
    outermost [ ... ] using a bracket-counting scan rather than regex.
    """
    text = text.strip()

    # Direct parse first
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Find the start of the first top-level JSON array
    start = text.find("[")
    if start == -1:
        logger.warning("Could not extract JSON array from response (len=%d)", len(text))
        return []

    # Walk forward counting brackets, respecting JSON strings
    depth = 0
    in_string = False
    escape_next = False
    for i, ch in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                try:
                    data = json.loads(candidate)
                    if isinstance(data, list):
                        return data
                except json.JSONDecodeError:
                    break

    logger.warning("Could not extract JSON array from response (len=%d)", len(text))
    return []


def generate_category_pairs(
    meta_prompt: str,
    generator_config: dict,
    category: str,
    n_pairs: int,
    dry_run: bool = False,
) -> list[dict]:
    prompt = build_generation_prompt(meta_prompt, category, n_pairs)

    if dry_run:
        logger.info("[DRY RUN] Would call %s for category %s (%d pairs)", generator_config.get("model_id"), category, n_pairs)
        return [{"prompt": f"[DRY RUN] cat={category} #{i}", "completion": f"[DRY RUN]", "category": category, "tags": []} for i in range(n_pairs)]

    logger.info("Generating %d pairs for category %s...", n_pairs, category)
    t0 = time.perf_counter()
    response = call_llm_with_config(prompt, generator_config, max_tokens=8192)
    elapsed = time.perf_counter() - t0
    logger.info("Generation done in %.1fs (%d chars)", elapsed, len(response or ""))

    if not response:
        logger.error("Empty response from generator for category %s", category)
        return []

    pairs = extract_json_array(response)
    logger.info("Extracted %d pairs for category %s", len(pairs), category)
    return pairs


# ── Judge ───────────────────────────────────────────────────────────────────

def build_judge_prompt(rubric: dict, pair: dict) -> str:
    template = rubric.get("judge_prompt_template", "")
    return template.replace("{prompt}", pair.get("prompt", "")).replace("{completion}", pair.get("completion", ""))


def judge_pair(pair: dict, rubric: dict, judge_config: dict, dry_run: bool = False) -> dict:
    if dry_run:
        return {"score": 1.0, "verdict": "PASS", "blocking_failures": [], "major_failures": [], "minor_failures": [], "comment": "DRY RUN"}

    prompt = build_judge_prompt(rubric, pair)
    response = call_llm_with_config(prompt, judge_config, max_tokens=512)

    if not response:
        return {"score": 0.0, "verdict": "FAIL", "comment": "empty judge response"}

    # Extract JSON from response
    try:
        # Try direct parse
        result = json.loads(response.strip())
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", response)
        if m:
            try:
                result = json.loads(m.group())
            except json.JSONDecodeError:
                return {"score": 0.0, "verdict": "FAIL", "comment": "unparseable judge response"}
        else:
            return {"score": 0.0, "verdict": "FAIL", "comment": "no JSON in judge response"}

    return result


# ── Pipeline ────────────────────────────────────────────────────────────────

def run_generation_pipeline(project: str, n_override: int | None = None, dry_run: bool = False) -> None:
    cfg = load_project_config(project)
    meta_prompt = load_meta_prompt(project)
    rubric = load_judge_rubric(project)

    ingestion = cfg.get("ingestion", {})
    generator_config = ingestion.get("pair_generator", {})
    judge_config = ingestion.get("pair_judge", {})
    min_score = judge_config.get("min_score", 0.75)
    blocking = judge_config.get("blocking_criteria", [])

    output_dir = Path(cfg["data"]["raw"])
    output_dir.mkdir(parents=True, exist_ok=True)

    all_passed: list[dict] = []
    all_rejected: list[dict] = []
    stats: dict[str, dict] = {}

    for cat, cat_info in CATEGORIES.items():
        n = n_override // len(CATEGORIES) if n_override else cat_info["n"]
        logger.info("=== Category %s — %s (%d pairs) ===", cat, cat_info["label"], n)

        pairs = generate_category_pairs(meta_prompt, generator_config, cat, n, dry_run)
        cat_passed = 0
        cat_rejected = 0

        for i, pair in enumerate(pairs):
            if not pair.get("prompt") or not pair.get("completion"):
                logger.warning("Skipping malformed pair %d (cat=%s)", i, cat)
                cat_rejected += 1
                continue

            judgment = judge_pair(pair, rubric, judge_config, dry_run)
            score = judgment.get("score", 0.0)
            verdict = judgment.get("verdict", "FAIL")

            # Blocking criteria check
            blocking_failures = judgment.get("blocking_failures", [])
            has_blocking = any(b in blocking_failures for b in blocking)

            pair["_judge"] = {
                "score": score,
                "verdict": verdict,
                "blocking_failures": blocking_failures,
                "major_failures": judgment.get("major_failures", []),
                "minor_failures": judgment.get("minor_failures", []),
                "comment": judgment.get("comment", ""),
            }

            if has_blocking or verdict == "FAIL" or score < min_score:
                all_rejected.append(pair)
                cat_rejected += 1
                logger.debug("REJECT pair %d (score=%.2f, blocking=%s)", i, score, blocking_failures)
            else:
                all_passed.append(pair)
                cat_passed += 1
                logger.debug("PASS pair %d (score=%.2f)", i, score)

        stats[cat] = {"generated": len(pairs), "passed": cat_passed, "rejected": cat_rejected}
        logger.info("Category %s: %d/%d passed", cat, cat_passed, len(pairs))

    # Save passed pairs as JSONL
    passed_path = output_dir / "pairs_raw.jsonl"
    with open(passed_path, "w", encoding="utf-8") as fh:
        for p in all_passed:
            # Strip internal judge metadata before saving
            clean = {k: v for k, v in p.items() if not k.startswith("_")}
            fh.write(json.dumps(clean, ensure_ascii=False) + "\n")

    # Save rejected for inspection
    rejected_path = output_dir / "pairs_rejected.jsonl"
    with open(rejected_path, "w", encoding="utf-8") as fh:
        for p in all_rejected:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Summary
    total_gen = sum(s["generated"] for s in stats.values())
    total_pass = sum(s["passed"] for s in stats.values())
    pass_rate = total_pass / total_gen if total_gen else 0.0

    logger.info("=" * 50)
    logger.info("GENERATION SUMMARY — %s", project)
    logger.info("Total generated : %d", total_gen)
    logger.info("Total passed    : %d  (%.0f%%)", total_pass, pass_rate * 100)
    logger.info("Total rejected  : %d", len(all_rejected))
    for cat, s in stats.items():
        logger.info("  Cat %s: %d/%d (%.0f%%)", cat, s["passed"], s["generated"], s["passed"] / s["generated"] * 100 if s["generated"] else 0)
    logger.info("Output: %s", passed_path)

    print(f"\n✓ {total_pass}/{total_gen} paires validées ({pass_rate:.0%})")
    print(f"  → {passed_path}")
    print(f"  → {rejected_path} ({len(all_rejected)} rejetées)")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate training pairs via Nova Pro + LLM judge")
    parser.add_argument("--project", required=True, help="Project ID (e.g. p01-tests-unitaires)")
    parser.add_argument("--n", type=int, default=None, help="Override total pair count")
    parser.add_argument("--dry-run", action="store_true", help="Skip LLM calls, generate mock pairs")
    args = parser.parse_args()

    run_generation_pipeline(args.project, n_override=args.n, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
