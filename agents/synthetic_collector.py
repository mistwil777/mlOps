"""Synthetic code collector — generates code examples via LLM from adapter description.

Produces the same neutral schema as doc_crawl:
  {description, code, lang, source_url, collected_at, adapter_name}

The rest of the pipeline (clean_raw, generate_bilingual, transform) sees no difference.

Usage:
    python -m agents.synthetic_collector \\
        --adapter-name jpa-hibernate-to-sqlalchemy-migrator \\
        --adapter-desc "Translates JPA/Hibernate entities to SQLAlchemy..." \\
        --lang java \\
        --n 100 \\
        --output datalake/projects/migrator/raw/synthetic_20260608T120000.jsonl
"""

from __future__ import annotations

import argparse
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import write_jsonl
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.synthetic_collector")

_LANGUAGES_YAML = Path("config/languages.yaml")
_BATCH_SIZE     = 5   # examples per LLM call — small batches = better quality
_MIN_CODE_LEN   = 30


def _load_concept_keywords(lang: str) -> list[str]:
    """Load concept keywords from languages.yaml, searching across all pairs."""
    if not _LANGUAGES_YAML.exists():
        return []
    with _LANGUAGES_YAML.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    keywords: list[str] = []
    for pair_cfg in cfg.get("pairs", {}).values():
        if pair_cfg.get("source_lang") == lang or pair_cfg.get("target_lang") == lang:
            keywords.extend(pair_cfg.get("concept_keywords", []))
    # Fallback: generic programming keywords if none found
    if not keywords:
        keywords = ["function", "class", "loop", "exception", "list", "map", "interface", "type"]
    return list(dict.fromkeys(keywords))  # deduplicate, preserve order


def _build_prompt(
    lang: str,
    adapter_desc: str,
    keywords: list[str],
    batch_size: int,
) -> str:
    kw_str = ", ".join(keywords)
    return (
        f"You are a technical documentation writer generating {lang} code examples.\n"
        f"Domain context: {adapter_desc}\n"
        f"Concepts to cover in this batch: {kw_str}\n\n"
        f"Generate exactly {batch_size} distinct, self-contained {lang} code examples "
        f"directly related to the domain above.\n"
        f"Each example must be complete and syntactically valid {lang} code.\n\n"
        f"For each example, use EXACTLY this format:\n"
        f"DESCRIPTION: <one sentence describing what the code does>\n"
        f"CODE:\n"
        f"```{lang}\n"
        f"<code here>\n"
        f"```\n"
        f"---\n\n"
        f"Output {batch_size} examples following this format, nothing else."
    )


def _parse_response(response: str, lang: str) -> list[dict[str, str]]:
    """Extract (description, code) pairs from LLM response."""
    results: list[dict[str, str]] = []

    # Split on --- separator
    blocks = re.split(r"\n---+\n?", response)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract description
        desc_match = re.search(r"DESCRIPTION:\s*(.+?)(?:\n|$)", block, re.IGNORECASE)
        desc = desc_match.group(1).strip() if desc_match else ""

        # Extract code block
        code_match = re.search(
            rf"```{re.escape(lang)}\n(.*?)```",
            block,
            re.DOTALL | re.IGNORECASE,
        )
        if not code_match:
            code_match = re.search(r"```\n(.*?)```", block, re.DOTALL)

        code = code_match.group(1).strip() if code_match else ""

        if desc and code and len(code) >= _MIN_CODE_LEN:
            results.append({"description": desc, "code": code})

    return results


def collect(
    adapter_name: str,
    adapter_desc: str,
    lang: str,
    n: int,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Generate n synthetic code records for the given adapter and language."""
    keywords = _load_concept_keywords(lang)
    rng = random.Random(seed)

    records: list[dict[str, Any]] = []
    source_url = "synthetic://" + adapter_name + "/" + lang
    ts = datetime.now(timezone.utc).isoformat()

    attempts = 0
    max_attempts = (n // _BATCH_SIZE + 2) * 3  # generous retry budget

    while len(records) < n and attempts < max_attempts:
        attempts += 1
        remaining = n - len(records)
        batch_size = min(_BATCH_SIZE, remaining)

        # Rotate keywords for diversity
        batch_keywords = rng.sample(keywords, min(3, len(keywords)))

        prompt = _build_prompt(lang, adapter_desc, batch_keywords, batch_size)
        response = call_llm(prompt, max_tokens=2048, temperature=0.3)

        if not response:
            logger.warning("LLM returned empty response (attempt %d)", attempts)
            continue

        parsed = _parse_response(response, lang)
        if not parsed:
            logger.debug("No examples parsed from response (attempt %d)", attempts)
            continue

        for item in parsed:
            if len(records) >= n:
                break
            records.append({
                "description":  item["description"],
                "code":         item["code"],
                "lang":         lang,
                "source_url":   source_url,
                "collected_at": ts,
                "adapter_name": adapter_name,
            })

        logger.info(
            "Generated %d/%d records (batch=%d, attempt=%d)",
            len(records), n, len(parsed), attempts,
        )

    if len(records) < n:
        logger.warning("Only generated %d/%d records after %d attempts", len(records), n, attempts)

    return records


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic code examples via LLM"
    )
    parser.add_argument("--adapter-name", required=True, dest="adapter_name",
                        help="Adapter name (used for tagging records and source_url)")
    parser.add_argument("--adapter-desc", required=True, dest="adapter_desc",
                        help="Adapter description (used as domain context for the LLM)")
    parser.add_argument("--lang",         required=True,
                        help="Target language (java, python, etc.)")
    parser.add_argument("--n",            type=int, default=100,
                        help="Number of records to generate (default: 100)")
    parser.add_argument("--output",       required=True,
                        help="Output JSONL path")
    parser.add_argument("--seed",         type=int, default=42)
    args = parser.parse_args()

    logger.info(
        "Generating %d synthetic %s records for adapter '%s'",
        args.n, args.lang, args.adapter_name,
    )

    records = collect(
        adapter_name=args.adapter_name,
        adapter_desc=args.adapter_desc,
        lang=args.lang,
        n=args.n,
        seed=args.seed,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(output, records)

    print("Collected " + str(len(records)) + " synthetic records → " + str(output))


if __name__ == "__main__":
    main()
