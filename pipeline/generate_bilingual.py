"""Generate target-language counterparts for monolingual clean_raw records.

Reads records in src_lang from clean_raw, calls the LLM backend to produce
target_lang code, and writes paired bilingual records (with a shared task_name)
to processed/bilingual/<adapter>.jsonl — ready for pipeline/transform.py with
the code_migration template.

Fully config-driven: src_lang and tgt_lang come from the project YAML.

Usage:
    python -m pipeline.generate_bilingual --project migrator
    python -m pipeline.generate_bilingual --project migrator --adapter java-to-python-translator --max 200
"""

from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.clean_raw import _cosine
from pipeline.utils.embeddings import embed as _embed
from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.generate_bilingual")

_PROJECTS_DIR = Path("config/projects")


def _load_project(project_id: str) -> dict[str, Any]:
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _task_name(description: str, code: str) -> str:
    """Stable identifier for a (description, code) record."""
    digest = hashlib.sha1(f"{description}|{code}".encode()).hexdigest()[:12]
    # Prefix with sanitised description snippet for readability
    slug = re.sub(r"[^a-z0-9]+", "_", description.lower())[:30].strip("_")
    return f"{slug}_{digest}"


def _build_prompt(
    src_lang: str,
    tgt_lang: str,
    src_code: str,
    description: str,
    adapter_description: str,
) -> str:
    return (
        f"You are an expert {src_lang}-to-{tgt_lang} code translator.\n"
        f"Context: {adapter_description}\n\n"
        f"Task description: {description}\n\n"
        f"Translate the following {src_lang} code to idiomatic {tgt_lang}. "
        f"Output ONLY the {tgt_lang} code, no explanation.\n\n"
        f"```{src_lang}\n{src_code}\n```"
    )


def _extract_code(response: str, lang: str) -> str:
    """Extract first fenced code block, fall back to raw response."""
    m = re.search(rf"```{re.escape(lang)}\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    return response.strip()


def generate_bilingual(
    project_id: str,
    adapter_name: str | None = None,
    max_per_adapter: int = 200,
    min_code_len: int = 30,
) -> dict[str, int]:
    cfg = _load_project(project_id)
    transform = cfg.get("transform", {})
    params = transform.get("params", {})
    src_lang: str = params.get("src_lang", "")
    tgt_lang: str = params.get("tgt_lang", "")

    if not src_lang or not tgt_lang:
        raise ValueError(
            f"Project '{project_id}' transform params missing src_lang/tgt_lang. "
            "Add: transform: {template: code_migration, params: {src_lang: ..., tgt_lang: ...}}"
        )

    adapters = cfg.get("adapters", [])
    if adapter_name:
        adapters = [a for a in adapters if a["name"] == adapter_name]
        if not adapters:
            raise ValueError(f"Adapter '{adapter_name}' not found in project '{project_id}'")

    # Only process fuse-strategy adapters — others don't need bilingual pairs
    fuse_adapters = [a for a in adapters if a.get("pair_strategy") == "fuse"]
    if not fuse_adapters:
        logger.info("No fuse-strategy adapters to process")
        return {}

    clean_raw_dir = Path(f"datalake/projects/{project_id}/processed/clean_raw")
    bilingual_dir = Path(f"datalake/projects/{project_id}/processed/bilingual")
    bilingual_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, int] = {}

    for adapter in fuse_adapters:
        name: str = adapter["name"]
        adapter_desc: str = adapter.get("description", "")
        input_file = clean_raw_dir / f"{name}.jsonl"

        if not input_file.exists():
            logger.warning("clean_raw not found for adapter '%s' — skipping", name)
            continue

        records = read_jsonl(input_file)
        src_records = [r for r in records if r.get("lang") == src_lang and r.get("code", "").strip()]
        tgt_existing = {r.get("code", "") for r in records if r.get("lang") == tgt_lang}

        logger.info(
            "Adapter '%s': %d %s records, %d %s already present",
            name, len(src_records), src_lang, len(tgt_existing), tgt_lang,
        )

        if not src_records:
            logger.warning("No %s records for adapter '%s' — skipping", src_lang, name)
            continue

        # Pre-filtre sémantique configurable (bilingual_sim_threshold dans adapter YAML)
        bil_sim_thr = adapter.get("bilingual_sim_threshold", 0.0)
        if bil_sim_thr > 0.0 and adapter_desc.strip():
            adv_list = _embed([adapter_desc[:500]])
            if adv_list:
                adv = adv_list[0]
                texts = [
                    (r.get("description", "") + " " + str(r.get("code", ""))[:400])
                    for r in src_records
                ]
                vecs = _embed(texts)
                if vecs:
                    before_bil = len(src_records)
                    src_records = [
                        r for r, v in zip(src_records, vecs)
                        if _cosine(v, adv) >= bil_sim_thr
                    ]
                    retention_pct = len(src_records) / max(before_bil, 1) * 100
                    logger.info(
                        "[%s] Bilingual pre-filter (thr=%.2f): %d/%d records retained (%.0f%%)",
                        name, bil_sim_thr, len(src_records), before_bil, retention_pct,
                    )
                    if not src_records:
                        logger.warning(
                            "[%s] Pre-filter removed all records — lower bilingual_sim_threshold", name
                        )
                        continue

        output_file = bilingual_dir / f"{name}.jsonl"
        existing = read_jsonl(output_file) if output_file.exists() else []
        seen_tasks = {r.get("task_name") for r in existing}

        generated = 0
        new_records: list[dict[str, Any]] = []
        ts = datetime.now(timezone.utc).isoformat()

        for rec in src_records[:max_per_adapter]:
            description = rec.get("description", "")
            src_code = rec.get("code", "")
            task = _task_name(description, src_code)

            if task in seen_tasks:
                continue

            prompt = _build_prompt(src_lang, tgt_lang, src_code, description, adapter_desc)
            response = call_llm(prompt, max_tokens=2048, temperature=0.1)

            if not response:
                logger.warning("[%s] LLM returned empty response — skipping", name)
                continue

            tgt_code = _extract_code(response, tgt_lang)
            if len(tgt_code) < min_code_len:
                logger.debug("[%s] Generated code too short (%d chars) — skipping", name, len(tgt_code))
                continue

            # Write both sides with the shared task_name
            base = {
                "task_name":    task,
                "description":  description,
                "source_url":   rec.get("source_url", ""),
                "adapter_name": name,
                "collected_at": ts,
                "generator":    "generate_bilingual",
            }
            new_records.append({**base, "lang": src_lang, "code": src_code})
            new_records.append({**base, "lang": tgt_lang, "code": tgt_code})
            seen_tasks.add(task)
            generated += 1

            if generated % 10 == 0:
                logger.info("[%s] %d/%d pairs generated", name, generated, min(len(src_records), max_per_adapter))

        if new_records:
            write_jsonl(output_file, existing + new_records)
            logger.info("[%s] %d new pairs → %s", name, generated, output_file)

        results[name] = generated

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate bilingual pairs via LLM for fuse-strategy adapters")
    parser.add_argument("--project",  required=True, help="Project ID")
    parser.add_argument("--adapter",  default=None,  help="Single adapter name (default: all fuse adapters)")
    parser.add_argument("--max",      type=int, default=200, dest="max_per_adapter",
                        help="Max source records to process per adapter (default: 200)")
    parser.add_argument("--min-code-len", type=int, default=30, dest="min_code_len",
                        help="Minimum generated code length in chars (default: 30)")
    args = parser.parse_args()

    results = generate_bilingual(
        project_id=args.project,
        adapter_name=args.adapter,
        max_per_adapter=args.max_per_adapter,
        min_code_len=args.min_code_len,
    )

    total = sum(results.values())
    for adapter, n in results.items():
        print(f"  {adapter}: {n} pairs")
    print(f"Total: {total} bilingual pairs generated")


if __name__ == "__main__":
    main()
