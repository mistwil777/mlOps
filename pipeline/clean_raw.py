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

logger = get_logger("mlops.clean_raw")

# ── Thresholds ────────────────────────────────────────────────────────────────

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

    augmented: list[dict[str, Any]] = []
    for r in eligible:
        if len(records) + len(augmented) >= target:
            break
        augmented.extend(augment_record(r, domain=domain))

    logger.info("Augmentation (%s): produced %d variants from %d eligible records", domain, len(augmented), len(eligible))
    log_metric("raw_augmented", len(augmented))
    return records + augmented


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
) -> dict[str, Any]:
    """Run the full clean_raw pipeline for one adapter. Returns report dict."""
    report: dict[str, Any] = {
        "adapter_name": adapter_name,
        "input_count":  len(records),
        "domain":       domain,
        "steps":        {},
    }

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

    # Load all raw records once, split by adapter
    all_raw = []
    for f in raw_dir.glob("*.jsonl"):
        all_raw.extend(read_jsonl(f))
    logger.info("Loaded %d raw records total from %s", len(all_raw), raw_dir)

    records_by_adapter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in all_raw:
        name = r.get("adapter_name")
        if name:
            records_by_adapter[name].append(r)

    # Cross-adapter reassignment (pass 1 — before per-adapter cleaning)
    if len(adapters) > 1 and not args.dry_run:
        adapter_descriptions = {a["name"]: a.get("description", "") for a in adapters}
        # We need embeddings for cross-adapter reassignment — embed all at once
        all_names_records = [(name, recs) for name, recs in records_by_adapter.items() if name in adapter_descriptions]
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

    domain = cfg.get("domain", "code")
    full_report: dict[str, Any] = {"project": args.project, "domain": domain, "adapters": {}}

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
