"""Clustering + coverage analysis.

Groups qualified records into semantic clusters to check whether the dataset
covers the expected categories for a given adapter.

Expected categories come from the advisor recommendation stored in the project config.
Actual coverage is determined by k-means clustering on embeddings.

Gaps (expected categories with no or few records) → targeted recommendations.

CLI:
    python -m pipeline.cluster --project <id> [--n-clusters 10] [--output <report.json>]
    python -m pipeline.cluster --input <file.jsonl> --n-clusters 10
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.cluster")

_PROJECTS_DIR = Path("config/projects")

from pipeline.utils.embeddings import embed as _embed_texts  # noqa: E402


# ── Embedding ──────────────────────────────────────────────────────────────────

def _embed(texts: list[str]) -> list[list[float]] | None:
    try:
        return _embed_texts(texts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Embedding unavailable: %s", exc)
        return None


def _record_text(r: dict[str, Any]) -> str:
    """Primary text representation for embedding."""
    if "task_name" in r and "code" in r:
        return f"{r.get('task_name', '')} {str(r.get('code', ''))[:300]}"
    if "description" in r:
        return f"{r.get('description', '')} {str(r.get('code', ''))[:200]}"
    if "prompt" in r:
        return f"{r.get('prompt', '')} {str(r.get('completion', ''))[:200]}"
    return str(r)[:400]


# ── K-Means (pure Python, no sklearn needed) ──────────────────────────────────

def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def _cosine_dist(a: list[float], b: list[float]) -> float:
    n = _norm(a) * _norm(b)
    return 1.0 - (_dot(a, b) / n) if n else 1.0


def _mean_vector(vecs: list[list[float]]) -> list[float]:
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]


def kmeans(
    vectors: list[list[float]],
    k: int,
    max_iter: int = 30,
    seed: int = 42,
) -> list[int]:
    """Spherical k-means. Returns cluster label for each vector."""
    if k >= len(vectors):
        return list(range(len(vectors)))

    rng = random.Random(seed)
    centroids = [vectors[i] for i in rng.sample(range(len(vectors)), k)]

    labels = [0] * len(vectors)
    for _ in range(max_iter):
        new_labels = [
            min(range(k), key=lambda c: _cosine_dist(v, centroids[c]))
            for v in vectors
        ]
        if new_labels == labels:
            break
        labels = new_labels
        for c in range(k):
            members = [vectors[i] for i, l in enumerate(labels) if l == c]
            if members:
                centroids[c] = _mean_vector(members)

    return labels


# ── LLM cluster labelling ──────────────────────────────────────────────────────

def _label_cluster(samples: list[str], adapter_context: str = "") -> str:
    """Ask Haiku to label a cluster from a few representative texts."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "unlabelled"

    texts = "\n---\n".join(s[:200] for s in samples[:5])
    prompt = (
        f"These are {len(samples)} data samples from a dataset for: {adapter_context or 'a fine-tuning adapter'}.\n\n"
        f"Samples:\n{texts}\n\n"
        "In 4 words or less, what programming concept or topic do these represent? Reply with the label only."
    )
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Cluster labelling failed: %s", exc)
        return "unlabelled"


# ── Coverage analysis ─────────────────────────────────────────────────────────

def _load_expected_categories(project_id: str, adapter_name: str | None = None) -> list[str]:
    """Load expected data categories from project config (set by advisor)."""
    p = _PROJECTS_DIR / f"{project_id}.yaml"
    if not p.exists():
        return []
    with p.open() as fh:
        cfg = yaml.safe_load(fh) or {}

    adapters = cfg.get("adapters", [])
    if adapter_name:
        adapters = [a for a in adapters if a.get("name") == adapter_name]

    categories: list[str] = []
    for a in adapters:
        categories.extend(a.get("expected_categories", []))
    return list(dict.fromkeys(categories))  # deduplicate, preserve order


def analyze_coverage(
    records: list[dict[str, Any]],
    n_clusters: int = 10,
    expected_categories: list[str] | None = None,
    adapter_context: str = "",
) -> dict[str, Any]:
    """Run clustering and coverage analysis.

    Returns a report dict with cluster labels, sizes, and gap analysis.
    Falls back to simple frequency analysis if embeddings are unavailable.
    """
    if not records:
        return {"error": "no_records", "clusters": [], "gaps": [], "coverage_score": 0.0}

    texts = [_record_text(r) for r in records]
    embeds = _embed(texts)

    if embeds is None:
        # Fallback: keyword frequency analysis (no embeddings)
        logger.warning("Embeddings unavailable — using keyword frequency fallback")
        return _keyword_coverage(records, expected_categories or [])

    effective_k = min(n_clusters, len(records))
    labels = kmeans(embeds, effective_k)

    # Group records by cluster
    clusters: list[dict[str, Any]] = []
    for c in range(effective_k):
        members = [i for i, l in enumerate(labels) if l == c]
        if not members:
            continue
        samples = [texts[i] for i in members[:5]]
        label   = _label_cluster(samples, adapter_context)
        clusters.append({
            "id":      c,
            "label":   label,
            "size":    len(members),
            "pct":     round(len(members) / len(records) * 100, 1),
            "samples": samples[:3],
        })

    # Sort by size descending
    clusters.sort(key=lambda x: x["size"], reverse=True)

    # Gap analysis against expected categories
    gaps: list[dict[str, Any]] = []
    if expected_categories:
        cluster_labels_lower = [c["label"].lower() for c in clusters]
        for cat in expected_categories:
            matched = any(cat.lower() in l or l in cat.lower() for l in cluster_labels_lower)
            if not matched:
                gaps.append({"category": cat, "found": False, "recommendation": f"Collect more data about: {cat}"})
            else:
                # Find the matching cluster
                for cl in clusters:
                    if cat.lower() in cl["label"].lower() or cl["label"].lower() in cat.lower():
                        if cl["pct"] < 5.0:
                            gaps.append({"category": cat, "found": True, "pct": cl["pct"],
                                         "recommendation": f"Under-represented ({cl['pct']}% of dataset): {cat}"})
                        break

    covered_cats = len(expected_categories) - len([g for g in gaps if not g.get("found", True)]) if expected_categories else len(clusters)
    total_cats   = max(len(expected_categories), 1) if expected_categories else len(clusters)
    coverage_score = round(covered_cats / total_cats, 3)

    return {
        "n_records":       len(records),
        "n_clusters":      len(clusters),
        "clusters":        clusters,
        "expected_categories": expected_categories or [],
        "gaps":            gaps,
        "coverage_score":  coverage_score,
    }


def _keyword_coverage(records: list[dict[str, Any]], expected: list[str]) -> dict[str, Any]:
    """Fallback when Ollama is unavailable: simple keyword matching."""
    all_text = " ".join(_record_text(r).lower() for r in records)
    gaps = []
    for cat in expected:
        keywords = cat.lower().split()
        found = any(kw in all_text for kw in keywords)
        if not found:
            gaps.append({"category": cat, "found": False, "recommendation": f"No records mentioning: {cat}"})

    return {
        "n_records":       len(records),
        "n_clusters":      0,
        "clusters":        [],
        "expected_categories": expected,
        "gaps":            gaps,
        "coverage_score":  round((len(expected) - len(gaps)) / max(len(expected), 1), 3),
        "note":            "keyword_fallback (Ollama unavailable)",
    }


def run_for_project(
    project_id: str,
    adapter_name: str | None = None,
    n_clusters: int = 10,
) -> dict[str, Any]:
    """Run coverage analysis for a project's qualified records."""
    qualified = Path(f"datalake/projects/{project_id}/processed/qualified.jsonl")
    if not qualified.exists():
        raise FileNotFoundError(f"{qualified} not found. Run pipeline/qualify.py first.")

    records = read_jsonl(qualified)
    if adapter_name:
        records = [r for r in records if r.get("adapter_name") == adapter_name]

    expected = _load_expected_categories(project_id, adapter_name)
    context  = adapter_name or project_id

    report = analyze_coverage(records, n_clusters, expected, context)

    # Persist report
    out_dir = Path(f"datalake/projects/{project_id}/processed")
    suffix  = f"_{adapter_name}" if adapter_name else ""
    out_path = out_dir / f"coverage_report{suffix}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    logger.info("Coverage report written to %s", out_path)

    return report


def _print_report(report: dict[str, Any]) -> None:
    print(f"\nRecords: {report['n_records']}  Clusters: {report['n_clusters']}  Coverage: {report['coverage_score']:.0%}")
    if report.get("note"):
        print(f"Note: {report['note']}")
    print("\nClusters (top 10):")
    for c in report["clusters"][:10]:
        bar = "█" * max(1, int(c["pct"] / 2))
        print(f"  {c['label']:<25} {c['size']:>5} records ({c['pct']:>5.1f}%) {bar}")
    if report["gaps"]:
        print(f"\nGaps ({len(report['gaps'])}):")
        for g in report["gaps"]:
            mark = "✗" if not g.get("found") else "⚠"
            print(f"  {mark} {g['recommendation']}")
    else:
        print("\nNo coverage gaps detected.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Clustering + coverage analysis")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project", help="Project ID")
    group.add_argument("--input",   help="Input JSONL file")
    parser.add_argument("--adapter",    help="Filter by adapter name")
    parser.add_argument("--n-clusters", type=int, default=10)
    parser.add_argument("--output",     help="JSON report output path")
    args = parser.parse_args()

    if args.project:
        report = run_for_project(args.project, args.adapter, args.n_clusters)
    else:
        records  = read_jsonl(args.input)
        report   = analyze_coverage(records, args.n_clusters)
        if args.output:
            Path(args.output).write_text(json.dumps(report, indent=2, ensure_ascii=False))

    _print_report(report)


if __name__ == "__main__":
    main()
