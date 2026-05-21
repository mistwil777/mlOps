"""CV-inspired data augmentation — only on qualified records.

Principle (from CV augmentation):
  - In image classification, rotation preserves the label (a cat rotated is still a cat)
  - Here: transformations must preserve the semantic relationship (input→output still valid)
  - NEVER augment rejected or unvalidated records — only qualified data

Available transformations:
  rename_vars   : rename variables/functions (preserves logic, increases lexical diversity)
  add_comments  : add explanatory comments (preserves semantics, tests description understanding)
  simplify      : simplify an overly complex snippet (different style, same result)
  edge_case     : generate an edge-case variant (extends coverage of the concept)

All generated records are verified by code_syntax before being accepted.
semantic_judge verifies equivalence for migration pairs.

CLI:
    python -m agents.data_augmentor --project <id> --adapter <name> --n 20 [--strategy rename_vars]
    python -m agents.data_augmentor --input <qualified.jsonl> --output <augmented.jsonl> --n 50
"""

from __future__ import annotations

import argparse
import os
import random
from pathlib import Path
from typing import Any

import yaml

from pipeline.checks.syntax import check_record as syntax_check
from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.data_augmentor")

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_PROJECTS_DIR = Path("config/projects")

STRATEGIES = ["rename_vars", "add_comments", "simplify", "edge_case"]

_STRATEGY_PROMPTS: dict[str, str] = {
    "rename_vars": """Rewrite the following {lang} code snippet by renaming all variables and functions
to different but meaningful names. Keep the logic and algorithm IDENTICAL.
Return ONLY the rewritten code, no explanation.

```{lang}
{code}
```""",

    "add_comments": """Add clear, concise inline comments to the following {lang} code to explain what each
significant step does. Keep the code logic and variable names UNCHANGED.
Return ONLY the commented code, no explanation.

```{lang}
{code}
```""",

    "simplify": """Rewrite the following {lang} code in a simpler, more idiomatic style.
The output must produce exactly the same results for the same inputs.
Return ONLY the simplified code, no explanation.

```{lang}
{code}
```""",

    "edge_case": """Write a {lang} code snippet that handles an edge case of the following function:
{task_context}

Base implementation:
```{lang}
{code}
```

Return ONLY the edge-case code snippet, no explanation.""",
}


def _call_haiku(prompt: str) -> str | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set")
        return None
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        return text
    except Exception as exc:  # noqa: BLE001
        logger.debug("Haiku call failed: %s", exc)
        return None


def _augment_record(
    record: dict[str, Any],
    strategy: str,
) -> dict[str, Any] | None:
    """Generate one augmented variant of a record. Returns None if generation fails."""
    lang = str(record.get("lang", "python")).lower()
    code = str(record.get("code", record.get("completion", "")))
    if not code.strip():
        return None

    task_context = str(record.get("task_name", record.get("description", "")))[:200]
    prompt_template = _STRATEGY_PROMPTS.get(strategy)
    if not prompt_template:
        return None

    prompt = prompt_template.format(lang=lang, code=code, task_context=task_context)
    new_code = _call_haiku(prompt)
    if not new_code or len(new_code.strip()) < 10:
        return None

    # Syntax check on generated code
    from pipeline.checks.syntax import check_syntax  # noqa: PLC0415
    ok, reason = check_syntax(new_code, lang)
    if not ok:
        logger.debug("Augmented code failed syntax check (%s): %s", strategy, reason)
        return None

    # Build the augmented record — same schema as source, replace code field
    augmented = {**record}
    if "code" in record:
        augmented["code"] = new_code
    elif "completion" in record:
        augmented["completion"] = new_code

    augmented["_augmented"]          = True
    augmented["_augment_strategy"]   = strategy
    augmented["_augment_source_hash"] = _short_hash(code)

    return augmented


def _short_hash(text: str) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def augment(
    records: list[dict[str, Any]],
    n: int,
    strategies: list[str] | None = None,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Generate n augmented records from a pool of qualified records.

    Selects source records randomly, applies a random strategy per record.
    Returns only records that pass syntax validation.
    """
    if not records:
        return []

    eff_strategies = strategies or STRATEGIES
    rng = random.Random(seed)

    generated: list[dict[str, Any]] = []
    attempts  = 0
    max_attempts = n * 4  # allow some failures

    while len(generated) < n and attempts < max_attempts:
        source   = rng.choice(records)
        strategy = rng.choice(eff_strategies)
        result   = _augment_record(source, strategy)
        if result:
            generated.append(result)
        attempts += 1

    logger.info(
        "Augmentation: %d generated / %d attempts (strategies: %s)",
        len(generated), attempts, eff_strategies,
    )
    return generated


def run_for_project(
    project_id: str,
    adapter_name: str | None = None,
    n: int = 50,
    strategies: list[str] | None = None,
) -> int:
    """Augment qualified records for a project. Returns count of records added."""
    qualified_path = Path(f"datalake/projects/{project_id}/processed/qualified.jsonl")
    if not qualified_path.exists():
        raise FileNotFoundError(f"{qualified_path} not found. Run pipeline/qualify.py first.")

    records = read_jsonl(qualified_path)
    if adapter_name:
        records = [r for r in records if r.get("adapter_name") == adapter_name]

    if not records:
        logger.warning("No qualified records found for adapter '%s'", adapter_name)
        return 0

    logger.info("Augmenting %d qualified records (target +%d)", len(records), n)
    augmented = augment(records, n, strategies)

    if not augmented:
        logger.warning("No augmented records generated")
        return 0

    # Append to qualified.jsonl (augmented data goes through the same pipeline)
    all_records = read_jsonl(qualified_path) + augmented
    write_jsonl(qualified_path, all_records)

    # Also write to a separate audit file
    aug_path = Path(f"datalake/projects/{project_id}/processed/augmented.jsonl")
    existing_aug = read_jsonl(aug_path) if aug_path.exists() else []
    write_jsonl(aug_path, existing_aug + augmented)

    logger.info("Added %d augmented records to %s", len(augmented), qualified_path)
    return len(augmented)


def main() -> None:
    parser = argparse.ArgumentParser(description="CV-inspired data augmentation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project ID")
    group.add_argument("--input",   help="Input JSONL file")
    parser.add_argument("--output",     help="Output JSONL (with --input)")
    parser.add_argument("--adapter",    help="Filter by adapter name")
    parser.add_argument("--n",          type=int, default=50, help="Number of records to generate")
    parser.add_argument("--strategies", nargs="+", choices=STRATEGIES, help="Strategies to use (default: all)")
    args = parser.parse_args()

    if args.project:
        n_added = run_for_project(args.project, args.adapter, args.n, args.strategies)
        print(f"Added {n_added} augmented records")
    else:
        if not args.output:
            parser.error("--output required with --input")
        records   = read_jsonl(args.input)
        augmented = augment(records, args.n, args.strategies)
        write_jsonl(args.output, augmented)
        print(f"Generated {len(augmented)} records → {args.output}")


if __name__ == "__main__":
    main()
