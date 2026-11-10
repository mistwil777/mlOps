"""Raw record cleaner — operates on monolingual records BEFORE pair formation.

Pipeline position:
    collect → [clean_raw] → pair_fuser → clean → extract → train

Per-adapter workflow:
    1. Exact deduplication      (hash on primary text fields)
    2. Heuristic scoring        (domain-aware: code quality OR text quality)
    3. Semantic deduplication   (cosine similarity on embeddings, threshold ~0.92)
    4. Relevance scoring        (cosine similarity to adapter description)
    5. Volume control           (keep top-N if excess; augment qualifying records if deficit)
    6. Coverage clustering      (K-means → detect thematic gaps → guide augmentation targets)
    7. Cross-adapter reassignment (records semantically closer to another adapter → move them)

Domain-aware scoring and augmentation (driven by project config `domain` field):
    domain=code  → code keyword check, Python AST validation, identifier renaming, loop swap
    domain=text  → entropy + length + boilerplate check, sentence reordering, paraphrase

CLI:
    python -m pipeline.clean_raw --project <id> [--adapter <name>] [--dry-run]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import random
import re
import textwrap
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger, log_metric
from pipeline.utils.embeddings import embed as _embed, current_backend as _embedding_backend
from pipeline.utils.ocr import ocr_records, OCR_CONFIDENCE_MIN

logger = get_logger("mlops.clean_raw")

# ── Thresholds ────────────────────────────────────────────────────────────────

# Semantic scoring weights (RAG similarity + KG entity overlap)
_SEM_RAG_WEIGHT      = 0.65
_SEM_KG_WEIGHT       = 0.35
# CDC quality.mode → percentile cutoff
_QUALITY_MODE_PERCENTILE: dict[str, float] = {
    "strict":     80.0,
    "balanced":   65.0,
    "permissive": 50.0,
}

QUALITY_THRESHOLD    = 0.55   # min heuristic score to keep a record
AUGMENT_THRESHOLD    = 0.65   # min score to be eligible for augmentation
SEM_DEDUP_THRESHOLD  = 0.92   # cosine similarity above which a record is a near-duplicate
RELEVANCE_THRESHOLD  = 0.05   # min cosine sim to adapter description — low because code vs prose
                               # naturally has low cosine similarity even when on-topic
VOLUME_RATIO_HIGH    = 1.1    # if collected > target × this → trim to target × 1.1 (hard cap)
VOLUME_RATIO_LOW     = 0.5    # if collected < target × this → trigger augmentation
MAX_AUG_PER_RECORD   = 2      # max synthetic variants produced per source record
N_CLUSTERS_DEFAULT   = 8      # K for coverage clustering


# ── Config ────────────────────────────────────────────────────────────────────

def _load_project_config(project: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open() as fh:
        return yaml.safe_load(fh)


# ── Record text extraction ────────────────────────────────────────────────────

def _record_text(r: dict[str, Any]) -> str:
    desc = r.get("description") or r.get("task_name") or r.get("completion") or ""
    code = str(r.get("code") or r.get("prompt") or "")[:400]
    return f"{desc} {code}".strip()


def _record_hash(r: dict[str, Any]) -> str:
    key = (r.get("description") or r.get("task_name") or r.get("prompt") or "") + str(r.get("code") or r.get("completion") or "")
    return hashlib.md5(key.encode()).hexdigest()


# ── 1. Exact deduplication ────────────────────────────────────────────────────

def dedup_exact(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in records:
        h = _record_hash(r)
        if h not in seen:
            seen.add(h)
            out.append(r)
    removed = len(records) - len(out)
    if removed:
        logger.info("Exact dedup: removed %d duplicates", removed)
        log_metric("raw_dedup_exact", removed)
    return out


# ── 2. Heuristic scoring (domain-aware) ──────────────────────────────────────

def _has_valid_python_syntax(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _primary_text(r: dict[str, Any]) -> str:
    """Return the main content field regardless of schema."""
    return str(r.get("code") or r.get("text") or r.get("content") or r.get("prompt") or "")


def _code_score(r: dict[str, Any]) -> float:
    code  = _primary_text(r)
    desc  = str(r.get("description") or r.get("task_name") or r.get("completion") or "")
    lang  = str(r.get("lang") or "").lower()

    if not code.strip():
        return 0.0

    score = 0.0

    n = len(code)
    if n < 10:
        score += 0.0
    elif n < 50:
        score += 0.2
    elif n <= 1500:
        score += 0.4
    else:
        score += 0.3

    CODE_KEYWORDS = ("def ", "class ", "return", "import", "public ", "private ",
                     "function", "=>", "void ", "int ", "if ", "for ", "while ")
    if any(kw in code for kw in CODE_KEYWORDS):
        score += 0.2

    if len(desc.strip()) > 20:
        score += 0.15

    if lang == "python":
        if _has_valid_python_syntax(code):
            score += 0.15
        else:
            score -= 0.1
    elif lang == "java":
        # Java-specific structural signals — no AST available, use regex heuristics
        JAVA_KEYWORDS = ("public ", "private ", "protected ", "class ", "interface ",
                         "void ", "static ", "extends ", "implements ", "@Override",
                         "System.out", "throws ", "new ", "return ")
        java_hits = sum(1 for kw in JAVA_KEYWORDS if kw in code)
        if java_hits >= 2:
            score += 0.15
        elif java_hits == 1:
            score += 0.05
        # Balanced braces = valid structure hint
        if code.count("{") > 0 and code.count("{") == code.count("}"):
            score += 0.05

    if code.count("\n") >= 3:
        score += 0.1

    if re.match(r"^[A-Z][a-z].*:$", code.strip().splitlines()[0]):
        score -= 0.15

    return min(1.0, max(0.0, score))


def _text_score(r: dict[str, Any]) -> float:
    """Domain-agnostic quality score for prose/document records."""
    import math as _math
    from collections import Counter as _Counter

    text = _primary_text(r)
    desc = str(r.get("description") or r.get("title") or r.get("task_name") or "")

    if not text.strip():
        return 0.0

    score = 0.0

    # Length (prefer 100–4000 chars for prose)
    n = len(text)
    if n < 50:
        score += 0.0
    elif n < 100:
        score += 0.2
    elif n <= 4000:
        score += 0.4
    else:
        score += 0.3

    # Shannon entropy — filters nav/boilerplate/repetitive text
    counts = _Counter(text)
    total  = len(text)
    entropy = -sum((c / total) * _math.log2(c / total) for c in counts.values())
    if entropy >= 4.0:
        score += 0.25
    elif entropy >= 3.0:
        score += 0.1
    else:
        score -= 0.15

    # Has a non-trivial description/title
    if len(desc.strip()) > 20:
        score += 0.15

    # Multi-sentence bonus (actual content, not a header)
    sentence_count = len(re.findall(r"[.!?]+", text))
    if sentence_count >= 3:
        score += 0.1

    # Penalty: navigation/boilerplate patterns
    _BOILERPLATE = (
        re.compile(r"lorem ipsum", re.IGNORECASE),
        re.compile(r"click here", re.IGNORECASE),
        re.compile(r"all rights reserved", re.IGNORECASE),
        re.compile(r"cookie(s)? policy", re.IGNORECASE),
        re.compile(r"^\s*(home|menu|search|login|sign in)\s*$", re.IGNORECASE | re.MULTILINE),
    )
    if any(p.search(text) for p in _BOILERPLATE):
        score -= 0.3

    return min(1.0, max(0.0, score))


def score_records(records: list[dict[str, Any]], domain: str = "code") -> list[dict[str, Any]]:
    scorer = _code_score if domain == "code" else _text_score
    out = []
    for r in records:
        s = scorer(r)
        out.append({**r, "_heuristic_score": round(s, 4)})
    low = sum(1 for r in out if r["_heuristic_score"] < QUALITY_THRESHOLD)
    logger.info("Heuristic scoring (%s): %d/%d records below threshold %.2f",
                domain, low, len(out), QUALITY_THRESHOLD)
    return out


# ── 3. Semantic deduplication ─────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x * x for x in a))
    nb  = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def dedup_semantic(
    records: list[dict[str, Any]],
    threshold: float = SEM_DEDUP_THRESHOLD,
) -> list[dict[str, Any]]:
    if len(records) < 2:
        return records

    texts = [_record_text(r) for r in records]
    vecs  = _embed(texts)
    if not vecs:
        logger.warning("Semantic dedup skipped — embeddings unavailable")
        return records

    kept_idx: list[int] = []
    for i, v in enumerate(vecs):
        if not any(_cosine(v, vecs[j]) >= threshold for j in kept_idx):
            kept_idx.append(i)

    removed = len(records) - len(kept_idx)
    if removed:
        logger.info("Semantic dedup (threshold=%.2f): removed %d near-duplicates", threshold, removed)
        log_metric("raw_dedup_semantic", removed)

    return [records[i] for i in kept_idx], [vecs[i] for i in kept_idx]


# ── 4. Relevance scoring ──────────────────────────────────────────────────────

def score_relevance(
    records: list[dict[str, Any]],
    vecs: list[list[float]],
    adapter_description: str,
) -> list[dict[str, Any]]:
    if not vecs or not adapter_description.strip():
        return records

    # TF-IDF builds a per-corpus vocabulary: embedding the description alone in a
    # separate call produces an incompatible vector space → cosines near zero for
    # everything.  Skip relevance scoring in that case and assign a neutral score.
    if _embedding_backend() == "tfidf":
        logger.warning(
            "Relevance scoring skipped (TF-IDF backend — cross-corpus comparison unreliable). "
            "Assigning neutral score 0.50 to all records."
        )
        return [{**r, "_relevance_score": 0.50} for r in records]

    adapter_vec = _embed([adapter_description[:500]])[0]
    out = []
    for r, v in zip(records, vecs):
        sim = _cosine(v, adapter_vec)
        out.append({**r, "_relevance_score": round(sim, 4)})

    low = sum(1 for r in out if r["_relevance_score"] < RELEVANCE_THRESHOLD)
    logger.info("Relevance scoring: %d/%d records below threshold %.2f",
                low, len(out), RELEVANCE_THRESHOLD)
    return out


# ── 5. Volume control ─────────────────────────────────────────────────────────

def _combined_score(r: dict[str, Any]) -> float:
    h = r.get("_heuristic_score", 0.0)
    rel = r.get("_relevance_score", 0.5)
    return 0.6 * h + 0.4 * rel


def apply_volume_control(
    records: list[dict[str, Any]],
    target: int,
) -> tuple[list[dict[str, Any]], str]:
    """Return (records_after_control, action: 'ok'|'trimmed'|'needs_augmentation')."""
    n = len(records)
    high_threshold = int(target * VOLUME_RATIO_HIGH)
    low_threshold  = int(target * VOLUME_RATIO_LOW)

    if n > high_threshold:
        # Cap at target × VOLUME_RATIO_HIGH (e.g. +10%), keep best by score
        sorted_recs = sorted(records, key=_combined_score, reverse=True)
        kept = sorted_recs[:high_threshold]
        logger.info("Volume trim: %d → %d (cap=%d target=%d)", n, len(kept), high_threshold, target)
        log_metric("raw_volume_trimmed", n - len(kept))
        return kept, "trimmed"
    elif n < low_threshold:
        logger.info("Volume deficit: %d records, target=%d — augmentation needed", n, target)
        return records, "needs_augmentation"
    else:
        return records, "ok"


# ── 6. Coverage clustering ────────────────────────────────────────────────────

def _kmeans(vectors: list[list[float]], k: int, max_iter: int = 30, seed: int = 42) -> list[int]:
    if k >= len(vectors):
        return list(range(len(vectors)))
    rng = random.Random(seed)
    centroids = [vectors[i] for i in rng.sample(range(len(vectors)), k)]
    labels = [0] * len(vectors)
    for _ in range(max_iter):
        new_labels = [min(range(k), key=lambda c: 1.0 - _cosine(v, centroids[c])) for v in vectors]
        if new_labels == labels:
            break
        labels = new_labels
        for c in range(k):
            members = [vectors[i] for i, lbl in enumerate(labels) if lbl == c]
            if members:
                dim = len(members[0])
                centroids[c] = [sum(m[d] for m in members) / len(members) for d in range(dim)]
    return labels


def analyze_coverage(
    records: list[dict[str, Any]],
    vecs: list[list[float]],
    n_clusters: int = N_CLUSTERS_DEFAULT,
) -> dict[str, Any]:
    if len(records) < n_clusters:
        n_clusters = max(2, len(records) // 2)

    labels = _kmeans(vecs, n_clusters)
    cluster_sizes: dict[int, int] = defaultdict(int)
    cluster_samples: dict[int, list[str]] = defaultdict(list)
    for i, lbl in enumerate(labels):
        cluster_sizes[lbl] += 1
        if len(cluster_samples[lbl]) < 3:
            cluster_samples[lbl].append(_record_text(records[i])[:150])

    avg_size = len(records) / n_clusters
    gaps = [c for c, sz in cluster_sizes.items() if sz < avg_size * 0.4]

    coverage = {
        "n_clusters":    n_clusters,
        "cluster_sizes": dict(cluster_sizes),
        "avg_size":      round(avg_size, 1),
        "gap_clusters":  gaps,
        "gap_samples":   {c: cluster_samples[c] for c in gaps},
        "labels":        labels,
    }
    logger.info("Coverage: %d clusters, %d gaps (under-represented)", n_clusters, len(gaps))
    return coverage


# ── 7. Cross-adapter reassignment ─────────────────────────────────────────────

def reassign_cross_adapter(
    records_by_adapter: dict[str, list[dict[str, Any]]],
    vecs_by_adapter: dict[str, list[list[float]]],
    adapter_descriptions: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Move records to the adapter they are semantically closest to."""
    if len(adapter_descriptions) < 2:
        return records_by_adapter

    adapter_vecs = {
        name: _embed([desc[:500]])[0]
        for name, desc in adapter_descriptions.items()
        if desc.strip()
    }

    result: dict[str, list[dict[str, Any]]] = {name: [] for name in records_by_adapter}
    moves = 0

    for current_adapter, records in records_by_adapter.items():
        vecs = vecs_by_adapter.get(current_adapter, [])
        for i, r in enumerate(records):
            if i >= len(vecs):
                result[current_adapter].append(r)
                continue
            v = vecs[i]
            best_adapter = max(adapter_vecs.keys(), key=lambda a: _cosine(v, adapter_vecs[a]))
            if best_adapter != current_adapter:
                # Only move if clearly better (> 0.15 margin)
                current_sim = _cosine(v, adapter_vecs[current_adapter])
                best_sim    = _cosine(v, adapter_vecs[best_adapter])
                if best_sim - current_sim > 0.15:
                    result[best_adapter].append({**r, "adapter_name": best_adapter, "_reassigned_from": current_adapter})
                    moves += 1
                    continue
            result[current_adapter].append(r)

    if moves:
        logger.info("Cross-adapter reassignment: %d records moved", moves)
        log_metric("raw_reassigned", moves)
    return result


# ── Augmentation (CV-inspired) ────────────────────────────────────────────────

def _augment_identifier_renaming(code: str, lang: str) -> str | None:
    """Replace concrete identifiers with generic ones (var_0, var_1, …)."""
    if lang == "python":
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None
        names: list[str] = sorted({
            node.id for node in ast.walk(tree)
            if isinstance(node, ast.Name) and not node.id.startswith("_")
            and node.id not in ("True", "False", "None", "self", "cls")
        })
        if not names:
            return None
        mapping = {name: f"var_{i}" for i, name in enumerate(names[:10])}
        result = code
        for old, new in mapping.items():
            result = re.sub(rf"\b{re.escape(old)}\b", new, result)
        # Validate the result still parses
        try:
            ast.parse(result)
            return result if result != code else None
        except SyntaxError:
            return None
    else:
        # Generic: rename simple camelCase/snake_case identifiers
        tokens = re.findall(r"\b[a-z][a-zA-Z0-9_]{2,}\b", code)
        uniq = list(dict.fromkeys(tokens))[:8]
        if not uniq:
            return None
        result = code
        for i, tok in enumerate(uniq):
            result = result.replace(tok, f"var{i}")
        return result if result != code else None


def _augment_loop_style(code: str, lang: str) -> str | None:
    """Swap for-range to while loop (Python only, simple cases)."""
    if lang != "python":
        return None
    pattern = re.compile(r"for (\w+) in range\((\w+)\):(.*?)(?=\n\S|\Z)", re.DOTALL)
    def replace(m: re.Match) -> str:
        var, limit, body = m.group(1), m.group(2), m.group(3)
        indent = re.match(r"\n(\s*)", body)
        ind = indent.group(1) if indent else "    "
        return f"{var} = 0\nwhile {var} < {limit}:{body}\n{ind}{var} += 1"
    result = pattern.sub(replace, code)
    if result == code:
        return None
    try:
        ast.parse(result)
        return result
    except SyntaxError:
        return None


def _augment_paraphrase_desc(description: str, lang: str) -> str | None:
    """Rewrite description with slight variation — no LLM, pure text transforms."""
    if not description or len(description) < 20:
        return None
    # Simple: shuffle sentence order if multi-sentence, or add/remove lead clause
    sentences = re.split(r"(?<=[.!?])\s+", description.strip())
    if len(sentences) >= 2:
        # Reverse order of first two sentences
        reordered = [sentences[1], sentences[0]] + sentences[2:]
        result = " ".join(reordered)
        return result if result != description else None
    # Single sentence: prepend or strip common intro phrases
    INTROS = ("This function ", "This method ", "This snippet ")
    for intro in INTROS:
        if description.startswith(intro):
            return description[len(intro):].capitalize()
    return f"Implementation that {description[0].lower()}{description[1:]}" if len(description) > 5 else None


def _augment_text_truncation(text: str) -> str | None:
    """Keep only the first 2/3 of a long prose record — simulates partial excerpt."""
    stripped = text.strip()
    if len(stripped) < 300:
        return None
    cutoff = int(len(stripped) * 0.67)
    # Snap to a sentence boundary
    boundary = stripped.rfind(".", 0, cutoff)
    if boundary < 100:
        return None
    result = stripped[:boundary + 1].strip()
    return result if result != stripped else None


# ── Missing import auto-fixer ─────────────────────────────────────────────────

_IMPORT_FIXES: list[tuple[str, str, str]] = [
    # (usage_pattern, import_check, import_line_to_add)
    (r'\bnp\.', "import numpy", "import numpy as np"),
    (r'\bpd\.', "import pandas", "import pandas as pd"),
    (r'\bpytest\.', "import pytest", "import pytest"),
    (r'\bos\.', "import os", "import os"),
    (r'\bsys\.', "import sys", "import sys"),
    (r'\bre\.', "import re", "import re"),
    (r'\bjson\.', "import json", "import json"),
    (r'\bdatetime\.', "import datetime", "from datetime import datetime"),
    (r'\bPath\(', "from pathlib", "from pathlib import Path"),
    (r'@dataclass', "from dataclasses", "from dataclasses import dataclass"),
    (r'\bTyping\b|\bList\[|\bDict\[|\bOptional\[|\bTuple\[', "from typing", "from typing import List, Dict, Optional, Tuple, Any"),
]


def fix_missing_imports(completion: str) -> tuple[str, list[str]]:
    """Add missing standard imports to a Python completion. Returns (fixed_completion, added_imports)."""
    import re as _re  # noqa: PLC0415
    if not completion or not completion.strip():
        return completion, []

    added: list[str] = []
    lines = completion.split("\n")
    existing = "\n".join(l for l in lines if l.strip().startswith(("import ", "from ")))

    imports_to_add: list[str] = []
    for pattern, check, import_line in _IMPORT_FIXES:
        if _re.search(pattern, completion) and check not in existing:
            imports_to_add.append(import_line)
            added.append(import_line)

    if not imports_to_add:
        return completion, []

    # Insert imports at the top, after any existing imports block
    insert_pos = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            insert_pos = i + 1
        elif insert_pos > 0:
            break

    for imp in reversed(imports_to_add):
        if imp not in existing:
            lines.insert(insert_pos, imp)

    return "\n".join(lines), added


def apply_missing_import_fixes(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Apply fix_missing_imports to all records that have a completion field. Returns (records, n_fixed)."""
    n_fixed = 0
    for r in records:
        completion = r.get("completion") or ""
        if not completion:
            continue
        fixed, added = fix_missing_imports(completion)
        if added:
            r["completion"] = fixed
            r["_import_fixes_applied"] = added
            n_fixed += 1
    return records, n_fixed


def augment_record(r: dict[str, Any], domain: str = "code") -> list[dict[str, Any]]:
    """Produce up to MAX_AUG_PER_RECORD augmented variants of a qualifying record."""
    lang = str(r.get("lang") or "").lower()
    now  = datetime.now(timezone.utc).isoformat()
    variants: list[dict[str, Any]] = []

    if domain == "code":
        code = str(r.get("code") or "")
        desc = str(r.get("description") or r.get("task_name") or "")

        renamed = _augment_identifier_renaming(code, lang)
        if renamed:
            variants.append({**r, "code": renamed, "_augmented": True,
                              "_aug_type": "identifier_renaming", "collected_at": now})
        if len(variants) < MAX_AUG_PER_RECORD:
            loop_swapped = _augment_loop_style(code, lang)
            if loop_swapped:
                variants.append({**r, "code": loop_swapped, "_augmented": True,
                                  "_aug_type": "loop_style", "collected_at": now})
        if not variants:
            new_desc = _augment_paraphrase_desc(desc, lang)
            if new_desc:
                variants.append({**r, "description": new_desc, "_augmented": True,
                                  "_aug_type": "desc_paraphrase", "collected_at": now})
    else:
        # Prose/text domain — sentence reorder then truncation
        text = _primary_text(r)
        desc = str(r.get("description") or r.get("title") or r.get("task_name") or "")

        new_desc = _augment_paraphrase_desc(desc, lang)
        if new_desc:
            variants.append({**r, "description": new_desc, "_augmented": True,
                              "_aug_type": "desc_paraphrase", "collected_at": now})
        if len(variants) < MAX_AUG_PER_RECORD:
            truncated = _augment_text_truncation(text)
            text_field = "text" if "text" in r else ("content" if "content" in r else "prompt")
            if truncated:
                variants.append({**r, text_field: truncated, "_augmented": True,
                                  "_aug_type": "text_truncation", "collected_at": now})

    return variants[:MAX_AUG_PER_RECORD]


def augment_batch(
    records: list[dict[str, Any]],
    target: int,
    coverage: dict[str, Any] | None = None,
    domain: str = "code",
) -> list[dict[str, Any]]:
    """Augment qualifying records until we approach target volume.

    If coverage gaps are known, prioritise records from under-represented clusters.
    """
    eligible = [r for r in records if r.get("_heuristic_score", 0) >= AUGMENT_THRESHOLD]
    if not eligible:
        logger.warning("No records above augmentation threshold %.2f — skipping augmentation", AUGMENT_THRESHOLD)
        return records

    if coverage and coverage.get("gap_clusters") and coverage.get("labels"):
        gap_set = set(coverage["gap_clusters"])
        gap_idx  = {i for i, lbl in enumerate(coverage["labels"]) if lbl in gap_set}
        priority = [r for i, r in enumerate(records) if i in gap_idx
                    and r.get("_heuristic_score", 0) >= AUGMENT_THRESHOLD]
        eligible = priority + [r for r in eligible if r not in priority]

    # Load validated bad patterns from memory store (best-effort)
    known_bad: list[dict] = []
    try:
        from pipeline.utils.memory_store import get_validated_corrections_for_phase
        known_bad = get_validated_corrections_for_phase("data_quality")
    except Exception:
        pass

    def _matches_bad_pattern(completion: str) -> bool:
        for c in known_bad:
            obs = (c.get("observed_value") or "")
            if len(obs) > 20 and obs[:100] in completion:
                return True
        return False

    augmented: list[dict[str, Any]] = []
    filtered_out = 0
    for r in eligible:
        if len(records) + len(augmented) >= target:
            break
        variants = augment_record(r, domain=domain)
        for v in variants:
            if _matches_bad_pattern(v.get("completion", "")):
                filtered_out += 1
            else:
                augmented.append(v)

    if filtered_out:
        logger.info("Augmentation: filtered %d variants matching known-bad patterns", filtered_out)
    logger.info("Augmentation (%s): produced %d variants from %d eligible records", domain, len(augmented), len(eligible))
    log_metric("raw_augmented", len(augmented))
    return records + augmented


# ── Semantic scoring (RAG + KG) ───────────────────────────────────────────────

def _load_semantic_context(
    project_id: str,
) -> tuple[object | None, object | None]:
    """Load IngestionRagIndex + ProjectKG for a project. Returns (rag, kg).

    Both are optional — returns (None, None) on any failure.  Callers must
    handle None gracefully (graceful degradation).
    """
    rag: object | None = None
    kg: object | None = None
    try:
        from pipeline.rag_index import IngestionRagIndex  # noqa: PLC0415
        rag = IngestionRagIndex(project_id)
        if rag.count == 0:
            logger.debug("Semantic context (RAG) empty for project '%s' — skipping semantic scoring", project_id)
            rag = None
    except Exception as exc:  # noqa: BLE001
        logger.debug("IngestionRagIndex unavailable: %s", exc)
    try:
        from pipeline.utils.knowledge_graph import ProjectKG  # noqa: PLC0415
        kg = ProjectKG(project_id)
        if not getattr(kg, "_available", False):
            kg = None
    except Exception as exc:  # noqa: BLE001
        logger.debug("ProjectKG unavailable: %s", exc)
    return rag, kg


def _rag_score_record(text: str, rag: object) -> float:
    """Max cosine score from top-3 RAG chunk hits. Returns 0.0 if no context."""
    try:
        scores: list[float] = rag.search_scores(text[:500], k=3)  # type: ignore[union-attr]
        return max(scores) if scores else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def _kg_score_record(text: str, kg: object) -> float:
    """Fraction of capitalised entity candidates found in KG. Returns 0.0 if no context."""
    candidates = list(set(re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", text)))[:15]
    if not candidates:
        return 0.0
    try:
        matched, total = kg.count_matching_entities(candidates)  # type: ignore[union-attr]
        return matched / total if total else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def score_semantic(
    records: list[dict[str, Any]],
    rag: object | None,
    kg: object | None,
) -> list[dict[str, Any]]:
    """Add _semantic_score, _rag_score, _kg_score to each record.

    If neither RAG nor KG is available, _semantic_score is set to None
    so downstream can skip the semantic filter entirely.
    """
    if rag is None and kg is None:
        logger.info("Semantic scoring skipped — no RAG/KG context available")
        return [{**r, "_semantic_score": None} for r in records]

    out: list[dict[str, Any]] = []
    for r in records:
        text = _record_text(r)
        rag_s = _rag_score_record(text, rag) if rag is not None else 0.5
        kg_s  = _kg_score_record(text, kg)   if kg  is not None else 0.5

        if rag is None:
            # Only KG available — use KG score exclusively
            composite = kg_s
        elif kg is None:
            # Only RAG available — use RAG score exclusively
            composite = rag_s
        else:
            composite = _SEM_RAG_WEIGHT * rag_s + _SEM_KG_WEIGHT * kg_s

        out.append({
            **r,
            "_rag_score":      round(rag_s, 4),
            "_kg_score":       round(kg_s, 4),
            "_semantic_score": round(composite, 4),
        })

    scores = [r["_semantic_score"] for r in out if r["_semantic_score"] is not None]
    if scores:
        avg = sum(scores) / len(scores)
        logger.info("Semantic scoring: %d records, avg=%.3f, min=%.3f, max=%.3f",
                    len(out), avg, min(scores), max(scores))
    return out


def _percentile_threshold(scores: list[float], mode: str) -> float:
    """Compute the score value at the percentile defined by quality.mode."""
    p = _QUALITY_MODE_PERCENTILE.get(mode, _QUALITY_MODE_PERCENTILE["balanced"])
    if not scores:
        return 0.0
    sorted_s = sorted(scores)
    idx = int(len(sorted_s) * p / 100)
    # Threshold = value AT that percentile — keep everything BELOW it, flag above
    # We want to keep the BEST records, so threshold = keep if score >= p(100-p)
    # Invert: reject if score < percentile(100 - p)
    keep_p = 100.0 - p
    keep_idx = max(0, int(len(sorted_s) * keep_p / 100))
    return sorted_s[keep_idx]


def filter_by_semantic_score(
    records: list[dict[str, Any]],
    mode: str,
    flagged_path: Path,
) -> list[dict[str, Any]]:
    """Split records into kept and flagged based on adaptive percentile threshold.

    Flagged records are appended to flagged_path (recovery queue) with their
    rejection reason and scores.  Returns the kept records only.
    """
    scored = [r for r in records if r.get("_semantic_score") is not None]
    unscored = [r for r in records if r.get("_semantic_score") is None]

    if not scored:
        return records  # nothing to filter

    scores = [r["_semantic_score"] for r in scored]
    threshold = _percentile_threshold(scores, mode)

    kept:    list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []

    for r in scored:
        if r["_semantic_score"] >= threshold:
            kept.append(r)
        else:
            flagged.append({
                **r,
                "_flagged_reason": "semantic_score_below_percentile",
                "_flagged_threshold": round(threshold, 4),
                "_flagged_quality_mode": mode,
            })

    if flagged:
        flagged_path.parent.mkdir(parents=True, exist_ok=True)
        with flagged_path.open("a", encoding="utf-8") as fh:
            for rec in flagged:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info(
            "Semantic filter (mode=%s, threshold=%.4f): kept=%d, flagged=%d → %s",
            mode, threshold, len(kept), len(flagged), flagged_path,
        )
        log_metric("raw_semantic_flagged", len(flagged))

    return kept + unscored


# ── Main pipeline per adapter ─────────────────────────────────────────────────

def clean_adapter_records(
    records: list[dict[str, Any]],
    adapter_name: str,
    adapter_description: str,
    target: int,
    n_clusters: int = N_CLUSTERS_DEFAULT,
    dry_run: bool = False,
    domain: str = "code",
    adapter_cfg: dict[str, Any] | None = None,
    task_type: str | None = None,
    project_id: str | None = None,
    quality_mode: str = "balanced",
    flagged_path: Path | None = None,
    rag_ctx: object | None = None,
    kg_ctx: object | None = None,
) -> dict[str, Any]:
    """Run the full clean_raw pipeline for one adapter. Returns report dict."""
    report: dict[str, Any] = {
        "adapter_name": adapter_name,
        "input_count":  len(records),
        "domain":       domain,
        "steps":        {},
    }

    # 0. OCR preprocessing — enrich records that have a file_path (PDF/image)
    ocr_records_present = any(r.get("file_path") for r in records)
    if ocr_records_present:
        min_conf = (adapter_cfg or {}).get("ocr_min_confidence", OCR_CONFIDENCE_MIN)
        records, ocr_excluded = ocr_records(records, min_confidence=min_conf)
        report["steps"]["ocr"] = {
            "kept": len(records),
            "excluded": len(ocr_excluded),
            "min_confidence": min_conf,
        }
        if ocr_excluded:
            log_metric("raw_ocr_excluded", len(ocr_excluded))
        logger.info("[%s] OCR: %d kept, %d excluded (confidence < %.2f)",
                    adapter_name, len(records), len(ocr_excluded), min_conf)

    # 1. Exact dedup
    records = dedup_exact(records)
    report["steps"]["exact_dedup"] = len(records)

    # 2. Heuristic scoring + filter
    records = score_records(records, domain=domain)
    before = len(records)
    quality_thr = (adapter_cfg or {}).get("quality_threshold", QUALITY_THRESHOLD)
    records = [r for r in records if r.get("_heuristic_score", 0) >= quality_thr]
    report["steps"]["heuristic_filter"] = len(records)
    logger.info("[%s] Heuristic filter: %d → %d", adapter_name, before, len(records))

    if not records:
        report["output_count"] = 0
        report["action"] = "empty_after_filter"
        return report

    # 3. Semantic dedup + get vecs
    result = dedup_semantic(records)
    if isinstance(result, tuple):
        records, vecs = result
    else:
        records, vecs = result, []
    report["steps"]["semantic_dedup"] = len(records)

    # 4. Relevance scoring
    if vecs:
        records = score_relevance(records, vecs, adapter_description)
        # Soft filter: only drop if very clearly irrelevant
        before = len(records)
        relevance_thr = (adapter_cfg or {}).get("relevance_threshold", RELEVANCE_THRESHOLD)
        records_filtered = [r for r in records if r.get("_relevance_score", 1.0) >= relevance_thr]
        vecs = [vecs[i] for i, r in enumerate(records) if r.get("_relevance_score", 1.0) >= relevance_thr]
        records = records_filtered
        report["steps"]["relevance_filter"] = len(records)
        logger.info("[%s] Relevance filter: %d → %d", adapter_name, before, len(records))

    # 4b. Semantic scoring (RAG + KG) — percentile-based, graceful degradation.
    # Skipped if records were already globally pre-filtered in main() (they carry _semantic_score).
    _already_scored = any(r.get("_semantic_score") is not None for r in records[:5])
    if not dry_run and not _already_scored and (rag_ctx is not None or kg_ctx is not None):
        records = score_semantic(records, rag_ctx, kg_ctx)
        if flagged_path is not None:
            before_sem = len(records)
            records = filter_by_semantic_score(records, quality_mode, flagged_path)
            report["steps"]["semantic_filter"] = {
                "mode":     quality_mode,
                "input":    before_sem,
                "kept":     len(records),
                "flagged":  before_sem - len(records),
            }
        # Strip internal scoring fields from vecs alignment (vecs may have drifted if
        # semantic filter removed records — rebuild vecs from surviving records)
        if vecs:
            # vecs was indexed by record position before semantic filter — realign
            # by embedding surviving records (batch is small at this stage)
            surviving_texts = [_record_text(r) for r in records]
            vecs = _embed(surviving_texts) or vecs

    # 5. Coverage analysis
    coverage = None
    if vecs and len(records) >= 4:
        coverage = analyze_coverage(records, vecs, n_clusters=min(n_clusters, len(records) // 2))
        report["steps"]["coverage"] = {
            "n_clusters": coverage["n_clusters"],
            "gap_clusters": coverage["gap_clusters"],
            "gap_sample_count": sum(len(v) for v in coverage["gap_samples"].values()),
        }

    # 6. Volume control
    records, action = apply_volume_control(records, target)
    report["steps"]["volume_control"] = {"action": action, "count": len(records)}

    if action == "needs_augmentation" and not dry_run:
        records = augment_batch(records, target=target, coverage=coverage, domain=domain)
        report["steps"]["augmentation"] = len(records) - report["steps"]["volume_control"]["count"]

    # 7. Deterministic fixers — loaded from config/fixers/registry.yaml (stage=raw)
    if not dry_run:
        resolved_task_type = (adapter_cfg or {}).get("task_type") or task_type
        lang_pair = (adapter_cfg or {}).get("lang_pair")
        try:
            from pipeline.fixers import apply_fixers
            records, fixer_stats = apply_fixers(
                records, stage="raw", domain=domain, task_type=resolved_task_type, lang_pair=lang_pair
            )
            if fixer_stats:
                report["steps"]["fixers_raw"] = fixer_stats
                for reason, count in fixer_stats.items():
                    logger.info("[%s] Fixer '%s': %d records patched", adapter_name, reason, count)
                    try:
                        from pipeline.utils.memory_store import mark_cluster_promoted
                        mark_cluster_promoted(reason)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("[%s] Fixer registry unavailable: %s", adapter_name, e)

    report["output_count"] = len(records)
    report["action"] = action
    return report, records


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Clean raw monolingual records before pair formation")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--adapter",  default=None, help="Process only this adapter (default: all)")
    parser.add_argument("--n-clusters", type=int, default=N_CLUSTERS_DEFAULT, dest="n_clusters")
    parser.add_argument("--dry-run",  action="store_true", dest="dry_run",
                        help="Score and report only — do not write output")
    parser.add_argument("--report",   default=None, help="Write JSON report to this path")
    args = parser.parse_args()

    cfg = _load_project_config(args.project)
    adapters: list[dict[str, Any]] = cfg.get("adapters", [])
    if args.adapter:
        adapters = [a for a in adapters if a["name"] == args.adapter]
        if not adapters:
            logger.error("Adapter '%s' not found in project '%s'", args.adapter, args.project)
            return

    raw_dir     = Path(f"datalake/projects/{args.project}/raw")
    cleaned_dir = Path(f"datalake/projects/{args.project}/processed/clean_raw")
    cleaned_dir.mkdir(parents=True, exist_ok=True)

    domain = cfg.get("domain", "code")
    quality_mode = cfg.get("quality", {}).get("mode", "balanced")
    flagged_path = Path(f"datalake/projects/{args.project}/flagged.jsonl")

    # Load shared semantic context FIRST — used both for pre-filter and per-adapter scoring
    rag_ctx, kg_ctx = _load_semantic_context(args.project)
    if rag_ctx or kg_ctx:
        logger.info("Semantic context loaded — RAG=%s, KG=%s",
                    "ready" if rag_ctx else "unavailable",
                    "ready" if kg_ctx else "unavailable")
    else:
        logger.info("No semantic context available — semantic scoring skipped")

    # Load all raw records once
    all_raw = []
    for f in raw_dir.glob("*.jsonl"):
        all_raw.extend(read_jsonl(f))
    logger.info("Loaded %d raw records total from %s", len(all_raw), raw_dir)

    # Global semantic pre-filter — runs BEFORE routing so only quality records are routed.
    # This ensures cross-adapter reassignment never sees semantically invalid records.
    if not args.dry_run and (rag_ctx is not None or kg_ctx is not None):
        all_raw = score_semantic(all_raw, rag_ctx, kg_ctx)
        before_global = len(all_raw)
        all_raw = filter_by_semantic_score(all_raw, quality_mode, flagged_path)
        logger.info("Global semantic pre-filter: %d → %d records (%d flagged)",
                    before_global, len(all_raw), before_global - len(all_raw))

    records_by_adapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    orphans: list[dict[str, Any]] = []
    for r in all_raw:
        name = r.get("adapter_name")
        if name:
            records_by_adapter[name].append(r)
        else:
            orphans.append(r)

    if orphans:
        adapter_names = [a["name"] for a in adapters]
        if len(adapter_names) == 1:
            # Single adapter — assign all orphans to it
            target = adapter_names[0]
            logger.info("Assigning %d orphan records (no adapter_name) → %s", len(orphans), target)
            for r in orphans:
                records_by_adapter[target].append({**r, "adapter_name": target})
        else:
            # Multiple adapters — let cross-adapter reassignment route them
            # Tag with a sentinel so they participate in embedding-based routing
            logger.info("Queuing %d orphan records for cross-adapter routing", len(orphans))
            for r in orphans:
                records_by_adapter["__orphan__"].append({**r, "adapter_name": "__orphan__"})

    # Cross-adapter reassignment (pass 1 — after semantic pre-filter, before per-adapter cleaning)
    if len(adapters) > 1 and not args.dry_run:
        adapter_descriptions = {a["name"]: a.get("description", "") for a in adapters}
        # We need embeddings for cross-adapter reassignment — embed all at once
        # Include orphans as a source to reassign; exclude the sentinel from target descriptions
        all_names_records = [(name, recs) for name, recs in records_by_adapter.items()
                             if name in adapter_descriptions or name == "__orphan__"]
        if all_names_records:
            flat_records = [r for _, recs in all_names_records for r in recs]
            flat_texts   = [_record_text(r) for r in flat_records]
            flat_vecs    = _embed(flat_texts)
            offset = 0
            vecs_by_adapter: dict[str, list[list[float]]] = {}
            for name, recs in all_names_records:
                vecs_by_adapter[name] = flat_vecs[offset:offset + len(recs)]
                offset += len(recs)
            records_by_adapter = dict(reassign_cross_adapter(
                dict(records_by_adapter), vecs_by_adapter, adapter_descriptions
            ))
            logger.info("Cross-adapter reassignment complete")

    full_report: dict[str, Any] = {
        "project":      args.project,
        "domain":       domain,
        "quality_mode": quality_mode,
        "adapters":     {},
    }

    for adapter in adapters:
        name   = adapter["name"]
        desc   = adapter.get("description", "")
        target = adapter.get("estimated_pairs", 500)
        recs   = records_by_adapter.get(name, [])

        logger.info("=== Processing adapter: %s (%d records, target=%d, domain=%s) ===",
                    name, len(recs), target, domain)

        if not recs:
            logger.warning("[%s] No records found — skipping", name)
            full_report["adapters"][name] = {"input_count": 0, "output_count": 0, "action": "no_data"}
            continue

        result = clean_adapter_records(
            recs, name, desc, target,
            n_clusters=args.n_clusters,
            dry_run=args.dry_run,
            domain=domain,
            adapter_cfg=adapter,
            project_id=args.project,
            quality_mode=quality_mode,
            flagged_path=flagged_path,
            rag_ctx=rag_ctx,
            kg_ctx=kg_ctx,
        )
        if isinstance(result, tuple):
            report, cleaned = result
        else:
            report, cleaned = result, recs

        full_report["adapters"][name] = report

        if not args.dry_run:
            out_path = cleaned_dir / f"{name}.jsonl"
            # Strip internal scoring fields before writing
            stripped = [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in cleaned
            ]
            write_jsonl(out_path, stripped)
            logger.info("[%s] Written %d records → %s", name, len(stripped), out_path)

    # Lookup pair_strategy per adapter for display context
    adapter_strategies = {a["name"]: a.get("pair_strategy", "doc_to_code") for a in cfg.get("adapters", [])}

    print("\n── clean_raw report ──────────────────────────────────────────")
    for name, rep in full_report["adapters"].items():
        inp  = rep.get("input_count", 0)
        out  = rep.get("output_count", 0)
        act  = rep.get("action", "")
        aug  = rep.get("steps", {}).get("augmentation", 0)
        gaps = rep.get("steps", {}).get("coverage", {}).get("gap_clusters", [])
        aug_str      = f"  +{aug} augmented" if aug else ""
        gap_str      = f"  {len(gaps)} coverage gaps" if gaps else ""
        strategy     = adapter_strategies.get(name, "doc_to_code")
        strategy_str = "  [→ awaiting pair_fuser]" if strategy == "fuse" else ""
        print(f"  {name:<45} {inp:>5} → {out:>5}{aug_str}{gap_str}  [{act}]{strategy_str}")
    print()

    if args.report:
        Path(args.report).write_text(json.dumps(full_report, indent=2, ensure_ascii=False))
        logger.info("Report written to %s", args.report)


if __name__ == "__main__":
    main()
