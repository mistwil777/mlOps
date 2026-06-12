"""Semantic pair matcher: form bilingual training pairs by matching descriptions.

Vision:
  Documentation pages teach the same concept in different languages.
  Each page has a natural-language description + a code block.
  Two descriptions in different languages that explain the same concept
  have a high cosine similarity when embedded together in the same vector space.
  => Match by description similarity, assemble pair {prompt: source_code, completion: target_code}.

No LLM generation. All acceptance decisions are made by software.
Generic: driven entirely by config/languages.yaml — no hardcoded language names.

Workflow:
  1. Load source-lang and target-lang records (neutral schema: description + code + lang)
  2. Filter: keep only records with both description and code
  3. Embed ALL descriptions in a single batch (shared vector space — critical)
  4. For each source record: find the best target match by cosine similarity on descriptions
  5. Optional boost: +0.05 per concept_keyword present in both descriptions
  6. If score >= threshold AND bijection constraint satisfied: assemble pair
  7. Software verification: ratio check + target-lang syntax check
  8. Write verified pairs to output
"""

from __future__ import annotations

import argparse
import ast
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.embeddings import embed as _embed
from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.semantic_pair_matcher")

_DEFAULT_THRESHOLD  = 0.75   # min cosine similarity on descriptions to accept a match
_MIN_CODE_CHARS     = 30     # min chars for a code block to be valid
_MIN_RATIO          = 0.10   # min len(completion) / len(prompt) ratio
_MAX_RATIO          = 10.0   # max ratio — reject aberrant completions
_KEYWORD_BOOST      = 0.05   # cosine boost per concept_keyword present in both descriptions


# ── Loaders ──────────────────────────────────────────────────────────────────

def _load_languages() -> dict[str, Any]:
    path = Path("config/languages.yaml")
    if not path.exists():
        raise FileNotFoundError("config/languages.yaml not found")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _load_pair_config(lang_pair: str) -> dict[str, Any]:
    cfg = _load_languages()
    pairs = cfg.get("pairs", {})
    if lang_pair not in pairs:
        raise ValueError(
            f"Unknown lang_pair '{lang_pair}'. "
            f"Available: {list(pairs.keys())}. "
            f"Add it to config/languages.yaml."
        )
    return pairs[lang_pair]


# ── Field extractors ──────────────────────────────────────────────────────────

def _description(r: dict[str, Any]) -> str:
    return (r.get("description") or r.get("task_name") or "").strip()


def _code(r: dict[str, Any]) -> str:
    return (r.get("code") or r.get("completion") or r.get("prompt") or "").strip()


# ── Cosine ────────────────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Keyword boost ─────────────────────────────────────────────────────────────

def _keyword_boost(desc_src: str, desc_tgt: str, keywords: list[str]) -> float:
    """Add KEYWORD_BOOST for each concept_keyword present in both descriptions."""
    if not keywords:
        return 0.0
    src_lower = desc_src.lower()
    tgt_lower = desc_tgt.lower()
    return sum(
        _KEYWORD_BOOST
        for kw in keywords
        if kw.lower() in src_lower and kw.lower() in tgt_lower
    )


# ── Software verifiers (reused from pair_fuser) ───────────────────────────────

def _verify_python(code: str) -> tuple[bool, str]:
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    try:
        ast.parse(code)
        return True, "ok"
    except SyntaxError as e:
        return False, f"syntax_error: {e}"


def _verify_java(code: str) -> tuple[bool, str]:
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    java_signals = re.compile(
        r'\b(class|interface|public|private|protected|void|int|String|return|import)\b'
    )
    if not java_signals.search(code):
        return False, "no_java_keywords"
    if code.count("{") != code.count("}"):
        return False, "unbalanced_braces"
    return True, "ok"


_VERIFIERS: dict[str, Any] = {
    "python": _verify_python,
    "java":   _verify_java,
}


def _verify_code(code: str, lang: str) -> tuple[bool, str]:
    verifier = _VERIFIERS.get(lang.lower())
    if verifier:
        return verifier(code)
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    return True, "ok"


def _verify_ratio(source: str, completion: str) -> tuple[bool, str]:
    if not source:
        return False, "empty_source"
    ratio = len(completion) / max(len(source), 1)
    if ratio < _MIN_RATIO:
        return False, f"completion_too_short (ratio={ratio:.2f})"
    if ratio > _MAX_RATIO:
        return False, f"completion_too_long (ratio={ratio:.2f})"
    return True, "ok"


# ── Core matcher ──────────────────────────────────────────────────────────────

class SemanticPairMatcher:
    """
    Match source-lang and target-lang records by description semantic similarity.
    Assembles (prompt=source_code, completion=target_code) pairs — no LLM call.
    All acceptance decisions are made by software.
    """

    def __init__(
        self,
        source_lang: str,
        target_lang: str,
        concept_keywords: list[str] | None = None,
        threshold: float = _DEFAULT_THRESHOLD,
    ):
        self.source_lang      = source_lang
        self.target_lang      = target_lang
        self.concept_keywords = concept_keywords or []
        self.threshold        = threshold

    def match(
        self,
        source_records: list[dict[str, Any]],
        target_records: list[dict[str, Any]],
        max_pairs: int = 500,
    ) -> list[dict[str, Any]]:
        # Filter to records with both description and code
        src = [r for r in source_records if _description(r) and _code(r)]
        tgt = [r for r in target_records if _description(r) and _code(r)]

        if not src or not tgt:
            logger.warning(
                "Not enough records with description+code: source=%d target=%d",
                len(src), len(tgt),
            )
            return []

        logger.info(
            "Embedding %d %s + %d %s descriptions in a single batch…",
            len(src), self.source_lang, len(tgt), self.target_lang,
        )

        # Single batch embedding — critical for shared vector space
        src_descs = [_description(r) for r in src]
        tgt_descs = [_description(r) for r in tgt]
        all_vecs  = _embed(src_descs + tgt_descs)
        src_vecs  = all_vecs[:len(src_descs)]
        tgt_vecs  = all_vecs[len(src_descs):]

        logger.info("Matching pairs (threshold=%.2f, keyword_boost=%.2f/kw)…",
                    self.threshold, _KEYWORD_BOOST)

        pairs: list[dict[str, Any]] = []
        used_tgt: set[int] = set()  # bijection: each target used at most once
        rejected = {"threshold": 0, "ratio": 0, "syntax": 0}

        for i, (src_rec, sv) in enumerate(zip(src, src_vecs)):
            if len(pairs) >= max_pairs:
                break

            src_desc  = _description(src_rec)
            src_code  = _code(src_rec)
            best_idx  = -1
            best_score = -1.0

            for j, (tgt_rec, tv) in enumerate(zip(tgt, tgt_vecs)):
                if j in used_tgt:
                    continue
                cos   = _cosine(sv, tv)
                boost = _keyword_boost(src_desc, _description(tgt_rec), self.concept_keywords)
                score = cos + boost
                if score > best_score:
                    best_score = score
                    best_idx   = j

            if best_idx < 0 or best_score < self.threshold:
                rejected["threshold"] += 1
                continue

            tgt_rec  = tgt[best_idx]
            tgt_code = _code(tgt_rec)

            # Software verification: ratio
            ok, reason = _verify_ratio(src_code, tgt_code)
            if not ok:
                rejected["ratio"] += 1
                logger.debug("[src=%d] rejected (ratio): %s", i, reason)
                continue

            # Software verification: target-lang syntax
            ok, reason = _verify_code(tgt_code, self.target_lang)
            if not ok:
                rejected["syntax"] += 1
                logger.debug("[src=%d] rejected (syntax/%s): %s", i, self.target_lang, reason)
                continue

            used_tgt.add(best_idx)

            logger.info(
                "[%d/%d] sim=%.3f — «%s» ↔ «%s»",
                len(pairs) + 1, min(len(src), max_pairs),
                best_score,
                src_desc[:60], _description(tgt_rec)[:60],
            )

            pairs.append({
                "prompt":           src_code,
                "completion":       tgt_code,
                "source_lang":      self.source_lang,
                "target_lang":      self.target_lang,
                "source_desc":      src_desc,
                "target_desc":      _description(tgt_rec),
                "similarity":       round(best_score, 4),
                "source_url":       src_rec.get("source_url"),
                "target_url":       tgt_rec.get("source_url"),
                "adapter_name":     src_rec.get("adapter_name"),
                "collector_type":   "semantic_matched",
                "collected_at":     datetime.now(timezone.utc).isoformat(),
            })

        logger.info(
            "Match complete: %d pairs | rejected: threshold=%d ratio=%d syntax=%d",
            len(pairs), rejected["threshold"], rejected["ratio"], rejected["syntax"],
        )
        return pairs

    def run(
        self,
        source_jsonl: Path,
        output: Path,
        adapter_name: str | None = None,
        max_pairs: int = 500,
    ) -> int:
        """Load source records, split by lang, match, write pairs."""
        all_records = read_jsonl(source_jsonl) if source_jsonl.exists() else []

        source_records = [
            r for r in all_records
            if r.get("lang", "").lower() == self.source_lang.lower()
        ]
        target_records = [
            r for r in all_records
            if r.get("lang", "").lower() == self.target_lang.lower()
        ]

        logger.info(
            "Loaded %d %s + %d %s records from %s",
            len(source_records), self.source_lang,
            len(target_records), self.target_lang,
            source_jsonl,
        )

        pairs = self.match(source_records, target_records, max_pairs=max_pairs)

        if adapter_name:
            for p in pairs:
                p["adapter_name"] = adapter_name

        if pairs:
            output.parent.mkdir(parents=True, exist_ok=True)
            existing = read_jsonl(output) if output.exists() else []
            seen = {(r.get("prompt", ""), r.get("completion", "")) for r in existing}
            new_unique = [p for p in pairs if (p["prompt"], p["completion"]) not in seen]
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d pairs → %s", len(new_unique), output)

        return len(pairs)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = _load_languages()
    pairs_cfg = cfg.get("pairs", {})

    parser = argparse.ArgumentParser(
        description="Match bilingual records into training pairs by description semantic similarity"
    )
    parser.add_argument("--pair",         required=True, choices=list(pairs_cfg.keys()),
                        help="Language pair key from config/languages.yaml")
    parser.add_argument("--source-jsonl", required=True, dest="source_jsonl",
                        help="Input JSONL with records from both languages (description+code+lang)")
    parser.add_argument("--output",       required=True,
                        help="Output JSONL for matched pairs")
    parser.add_argument("--threshold",    type=float, default=None,
                        dest="threshold",
                        help="Min cosine similarity (default from languages.yaml or 0.75)")
    parser.add_argument("--max-pairs",    type=int, default=500, dest="max_pairs")
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    args = parser.parse_args()

    pcfg     = pairs_cfg[args.pair]
    matching = pcfg.get("matching", {})

    threshold = (
        args.threshold
        or matching.get("min_similarity")
        or _DEFAULT_THRESHOLD
    )
    keywords = pcfg.get("concept_keywords", [])

    matcher = SemanticPairMatcher(
        source_lang=pcfg["source_lang"],
        target_lang=pcfg["target_lang"],
        concept_keywords=keywords,
        threshold=threshold,
    )
    n = matcher.run(
        source_jsonl=Path(args.source_jsonl),
        output=Path(args.output),
        adapter_name=args.adapter_name,
        max_pairs=args.max_pairs,
    )
    print(f"Matched {n} pairs → {args.output}")


if __name__ == "__main__":
    main()
