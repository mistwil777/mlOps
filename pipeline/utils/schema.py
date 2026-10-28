"""Canonical pair schema — stable output format for all transform functions.

Every transform (code_migration, doc_to_code, qa, semantic, ...) must produce
CanonicalPair instances. This decouples the data source from downstream steps
(clean, scoring, evaluate, fine-tuning) so that adding a new project (HR, Legal,
Defense) never requires changes outside the transform layer.

Usage:
    from pipeline.utils.schema import CanonicalPair, canonicalize, validate_pairs

    pairs = [CanonicalPair(prompt="...", completion="...", adapter_name="foo")]
    issues = validate_pairs(pairs)
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class CanonicalPair:
    # ── Required ──────────────────────────────────────────────────────────────
    prompt: str
    completion: str

    # ── Strongly recommended ──────────────────────────────────────────────────
    adapter_name: str | None = None   # which LoRA adapter this pair trains
    project_id: str | None = None     # project this pair belongs to

    # ── Provenance ────────────────────────────────────────────────────────────
    source_url: str | None = None     # URL or file path of the source document
    source_type: str | None = None    # "doc_crawl" | "web_crawl" | "manual" | "synthetic" | "multilang_wiki"
    collected_at: str = field(        # ISO-8601 UTC
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ── Language / domain ─────────────────────────────────────────────────────
    lang: str | None = None           # primary language ("java", "python", "fr", …)
    lang_pair: str | None = None      # e.g. "java-python" for migration tasks
    domain: str | None = None         # "code" | "legal" | "hr" | "maintenance" | …

    # ── Quality signals ───────────────────────────────────────────────────────
    quality_score: float | None = None   # 0–1, assigned by scorer
    similarity: float | None = None      # semantic similarity between source/target desc

    # ── Freeform metadata (source-specific) ───────────────────────────────────
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None values to keep JSONL compact, but always keep required fields
        return {k: v for k, v in d.items() if v is not None or k in ("prompt", "completion")}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CanonicalPair":
        known = {f for f in CanonicalPair.__dataclass_fields__}  # type: ignore[attr-defined]
        extra = {k: v for k, v in d.items() if k not in known}
        base  = {k: v for k, v in d.items() if k in known}
        if extra:
            base.setdefault("meta", {}).update(extra)
        return CanonicalPair(**base)


def canonicalize(raw: dict[str, Any], defaults: dict[str, Any] | None = None) -> CanonicalPair:
    """Normalize a raw transform output dict into a CanonicalPair.

    Handles the legacy field variations produced by the three transform modes:
    - matched:  {prompt, completion, source_url, adapter_name, meta, collected_at}
    - direct:   {prompt, completion, lang, source_url, adapter_name, meta, collected_at}
    - semantic: {prompt, completion, source_lang, target_lang, similarity, source_url, ...}
    """
    merged: dict[str, Any] = dict(defaults or {})
    merged.update(raw)

    # Normalize lang_pair from source_lang/target_lang (semantic transform output)
    if "source_lang" in merged and "target_lang" in merged and not merged.get("lang_pair"):
        merged["lang_pair"] = f"{merged['source_lang']}-{merged['target_lang']}"
        merged.setdefault("lang", merged["source_lang"])

    # Absorb legacy meta keys into meta dict
    meta_in: dict[str, Any] = merged.pop("meta", {}) or {}
    legacy_meta_keys = {"src_lang", "tgt_lang", "tokens", "match_by", "similarity"}
    for k in legacy_meta_keys:
        if k in merged:
            meta_in[k] = merged.pop(k)

    # Pull similarity out of meta if present
    if "similarity" not in merged and "similarity" in meta_in:
        merged["similarity"] = meta_in.pop("similarity")

    # Carry over source_url from target_url if missing
    if not merged.get("source_url") and merged.get("target_url"):
        merged["source_url"] = merged.pop("target_url")
    else:
        merged.pop("target_url", None)

    # Drop fields that don't belong in CanonicalPair
    drop = {"source_lang", "target_lang", "source_desc", "target_desc", "target_url"}
    for k in drop:
        merged.pop(k, None)

    merged["meta"] = meta_in
    return CanonicalPair.from_dict(merged)


def validate_pairs(
    pairs: list[CanonicalPair],
    require_adapter: bool = True,
) -> list[dict[str, Any]]:
    """Return a list of validation issues (empty = all good).

    Each issue: {"index": int, "field": str, "message": str}
    """
    issues: list[dict[str, Any]] = []
    for i, p in enumerate(pairs):
        if not p.prompt or not p.prompt.strip():
            issues.append({"index": i, "field": "prompt", "message": "Empty prompt"})
        if not p.completion or not p.completion.strip():
            issues.append({"index": i, "field": "completion", "message": "Empty completion"})
        if require_adapter and not p.adapter_name:
            issues.append({"index": i, "field": "adapter_name", "message": "Missing adapter_name — pair cannot be routed to a LoRA adapter"})
        if p.quality_score is not None and not (0.0 <= p.quality_score <= 1.0):
            issues.append({"index": i, "field": "quality_score", "message": f"Out of range: {p.quality_score}"})
    return issues
