"""Transform collected records into training pairs.

Reads neutral collector output (e.g. {task_name, lang, code} from rosetta_collector,
or {description, code, lang} from doc_collector) and produces {prompt, completion}
pairs ready for clean.py and extract.py.

The transformation is driven by config/transforms/<template_id>.yaml and project params.

Usage:
    python -m pipeline.transform --project <project_id> --input <raw.jsonl> --output <pairs.jsonl>
    python -m pipeline.transform --template code_migration --params src_lang=python tgt_lang=java \\
        --input raw.jsonl --output pairs.jsonl
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.transform")

_TRANSFORMS_DIR = Path("config/transforms")
_PROJECTS_DIR   = Path("config/projects")


def _load_template(template_id: str) -> dict[str, Any]:
    path = _TRANSFORMS_DIR / f"{template_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Transform template not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _load_project_transform(project_id: str) -> tuple[str, dict[str, str]]:
    """Return (template_id, params) from project config."""
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")
    with path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    transform_cfg = cfg.get("transform")
    if not transform_cfg:
        raise ValueError(
            f"Project '{project_id}' has no 'transform' section in its config. "
            "Add: transform: {template: <id>, params: {...}}"
        )
    return transform_cfg["template"], transform_cfg.get("params", {})


def _resolve(template: str, params: dict[str, str]) -> str:
    """Replace {{key}} placeholders with param values."""
    def replacer(m: re.Match) -> str:  # type: ignore[type-arg]
        key = m.group(1).strip()
        if key not in params:
            raise ValueError(f"Template placeholder '{{{{ {key} }}}}' has no matching param")
        return params[key]
    return re.sub(r"\{\{([^}]+)\}\}", replacer, template)


def _transform_matched(
    records: list[dict[str, Any]],
    schema: dict[str, Any],
    params: dict[str, str],
    instruction_template: str,
) -> list[dict[str, Any]]:
    """
    Join records by match_by field, then pair input-side vs output-side records.

    Example: records have {task_name, lang, code}.
    schema.match_by = "task_name"
    schema.input_filter  = {field: lang, value: "{{src_lang}}"}  → lang == "Python"
    schema.output_filter = {field: lang, value: "{{tgt_lang}}"}  → lang == "Java"
    """
    match_by     = schema["match_by"]
    input_field  = schema["input_field"]
    output_field = schema["output_field"]
    in_filter    = schema.get("input_filter", {})
    out_filter   = schema.get("output_filter", {})

    in_fv  = _resolve(in_filter.get("value", ""),  params) if in_filter  else None
    out_fv = _resolve(out_filter.get("value", ""), params) if out_filter else None
    in_ff  = in_filter.get("field")  if in_filter  else None
    out_ff = out_filter.get("field") if out_filter else None

    # Group by match key
    by_key: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: {"in": [], "out": []})
    for rec in records:
        key = rec.get(match_by)
        if not key:
            continue
        field_val = str(rec.get(in_ff, "")) if in_ff else None
        if in_ff and in_fv and field_val == in_fv:
            by_key[key]["in"].append(rec)
        out_val = str(rec.get(out_ff, "")) if out_ff else None
        if out_ff and out_fv and out_val == out_fv:
            by_key[key]["out"].append(rec)

    pairs: list[dict[str, Any]] = []
    for key, sides in by_key.items():
        if not sides["in"] or not sides["out"]:
            continue
        # Take first of each side (multi-implementation tasks: keep first)
        in_rec  = sides["in"][0]
        out_rec = sides["out"][0]
        input_code  = in_rec.get(input_field, "")
        output_code = out_rec.get(output_field, "")
        if not input_code or not output_code:
            continue

        instruction = _resolve(instruction_template, {**params, "input": input_code})
        adapter_name = in_rec.get("adapter_name") or out_rec.get("adapter_name")
        pairs.append({
            "prompt":       instruction,
            "completion":   output_code,
            "source_url":   in_rec.get("source_url"),
            "adapter_name": adapter_name,
            "meta": {
                match_by:   key,
                "src_lang": params.get("src_lang"),
                "tgt_lang": params.get("tgt_lang"),
                "tokens":   len(instruction.split()) + len(output_code.split()),
            },
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })

    return pairs


def _transform_direct(
    records: list[dict[str, Any]],
    schema: dict[str, Any],
    params: dict[str, str],
    instruction_template: str,
) -> list[dict[str, Any]]:
    """
    Direct field rename — one record → one pair.
    Example: {description, code, lang} → {prompt, completion}.
    """
    input_field  = schema["input_field"]
    output_field = schema["output_field"]
    lang_field   = schema.get("lang_field")

    pairs: list[dict[str, Any]] = []
    for rec in records:
        input_val  = rec.get(input_field, "")
        output_val = rec.get(output_field, "")
        if not input_val or not output_val:
            continue

        lang = rec.get(lang_field, "") if lang_field else params.get("lang", "")
        resolved_params = {**params, "lang": lang, "input": input_val}
        instruction = _resolve(instruction_template, resolved_params)

        pairs.append({
            "prompt":       instruction,
            "completion":   output_val,
            "lang":         lang,
            "source_url":   rec.get("source_url"),
            "adapter_name": rec.get("adapter_name"),
            "meta": {
                "lang":   lang,
                "tokens": len(instruction.split()) + len(output_val.split()),
            },
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })

    return pairs


def _transform_semantic(
    records: list[dict[str, Any]],
    schema: dict[str, Any],
    params: dict[str, str],
    instruction_template: str,
) -> list[dict[str, Any]]:
    """
    Semantic join: match records from two languages by cosine similarity
    on a natural-language description field.

    Delegates to agents.semantic_pair_matcher.SemanticPairMatcher so that
    both transform.py and the standalone CLI share the same logic.
    """
    from agents.semantic_pair_matcher import SemanticPairMatcher  # noqa: PLC0415

    src_lang   = _resolve(schema.get("input_filter", {}).get("value", ""), params)
    tgt_lang   = _resolve(schema.get("output_filter", {}).get("value", ""), params)
    threshold  = float(schema.get("min_similarity", 0.75))
    input_filt = schema.get("input_filter", {})
    out_filt   = schema.get("output_filter", {})
    in_ff      = input_filt.get("field")
    out_ff     = out_filt.get("field")

    src_records = [r for r in records if in_ff  and str(r.get(in_ff,  "")).lower() == src_lang.lower()]
    tgt_records = [r for r in records if out_ff and str(r.get(out_ff, "")).lower() == tgt_lang.lower()]

    matcher = SemanticPairMatcher(
        source_lang=src_lang,
        target_lang=tgt_lang,
        threshold=threshold,
    )
    raw_pairs = matcher.match(src_records, tgt_records)

    # Wrap each raw pair in the instruction template for consistency with other transforms
    pairs: list[dict[str, Any]] = []
    for p in raw_pairs:
        instruction = _resolve(instruction_template, {**params, "input": p["prompt"]}).strip()
        pairs.append({
            "prompt":       instruction,
            "completion":   p["completion"],
            "source_lang":  p["source_lang"],
            "target_lang":  p["target_lang"],
            "source_desc":  p.get("source_desc"),
            "target_desc":  p.get("target_desc"),
            "similarity":   p.get("similarity"),
            "source_url":   p.get("source_url"),
            "target_url":   p.get("target_url"),
            "meta": {
                "src_lang":   src_lang,
                "tgt_lang":   tgt_lang,
                "similarity": p.get("similarity"),
            },
            "collected_at": p.get("collected_at"),
        })

    return pairs


def transform(
    records: list[dict[str, Any]],
    template_id: str,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    """Apply a transform template to a list of collector records.

    Args:
        records:     Raw collector output.
        template_id: Transform template ID (maps to config/transforms/<id>.yaml).
        params:      Template parameters, e.g. {"src_lang": "Python", "tgt_lang": "Java"}.

    Returns:
        List of {prompt, completion, meta, ...} training pairs.
    """
    tmpl = _load_template(template_id)
    schema               = tmpl["input_schema"]
    instruction_template = tmpl["instruction_template"]

    for required in tmpl.get("params_required", []):
        if required not in params:
            raise ValueError(
                f"Transform '{template_id}' requires param '{required}'. "
                f"Provided: {list(params.keys())}"
            )

    if schema.get("match_by") == "semantic_description":
        pairs = _transform_semantic(records, schema, params, instruction_template)
    elif "match_by" in schema:
        pairs = _transform_matched(records, schema, params, instruction_template)
    else:
        pairs = _transform_direct(records, schema, params, instruction_template)

    logger.info("Transform '%s': %d records → %d pairs", template_id, len(records), len(pairs))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transform collector records into training pairs")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--project",  help="Project ID — reads template + params from project config")
    source.add_argument("--template", help="Transform template ID (config/transforms/<id>.yaml)")

    parser.add_argument("--params",   nargs="*", default=[],
                        help="Template params as key=value pairs (only with --template)")
    parser.add_argument("--input",    required=True, help="Input JSONL (collector output)")
    parser.add_argument("--output",   required=True, help="Output JSONL (training pairs)")
    args = parser.parse_args()

    if args.project:
        template_id, params = _load_project_transform(args.project)
    else:
        template_id = args.template
        params = {}
        for kv in args.params:
            k, _, v = kv.partition("=")
            params[k.strip()] = v.strip()

    records = read_jsonl(Path(args.input))
    logger.info("Loaded %d records from %s", len(records), args.input)

    pairs = transform(records, template_id, params)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(output, pairs)
    print(f"Transformed {len(records)} records → {len(pairs)} pairs → {args.output}")


if __name__ == "__main__":
    main()
