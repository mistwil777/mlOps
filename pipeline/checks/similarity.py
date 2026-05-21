"""Cosine + Euclidean similarity between record embeddings.

Uses nomic-embed-text via Ollama (port 11434). Falls back to character-level
Jaccard similarity when Ollama is unavailable (e.g. CI environments).

Primary uses:
  1. Near-duplicate detection (cosine ≥ threshold → same record seen twice)
  2. Migration pair alignment (input code and output code should be semantically similar)

CLI:
    python -m pipeline.checks.similarity --input <file.jsonl> --mode dedup [--threshold 0.97]
    python -m pipeline.checks.similarity --text1 "..." --text2 "..."
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.checks.similarity")

_OLLAMA_URL = "http://localhost:11434"
_EMBED_MODEL = "nomic-embed-text"
_DEFAULT_DEDUP_THRESHOLD = 0.97
_DEFAULT_ALIGN_THRESHOLD = 0.85


def _embed_via_ollama(texts: list[str]) -> list[list[float]] | None:
    try:
        import httpx  # noqa: PLC0415

        resp = httpx.post(
            f"{_OLLAMA_URL}/api/embed",
            json={"model": _EMBED_MODEL, "input": texts},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ollama embed unavailable: %s", exc)
        return None


def _jaccard(a: str, b: str) -> float:
    """Character-level bigram Jaccard similarity."""
    def bigrams(s: str) -> set[str]:
        return {s[i:i+2] for i in range(len(s) - 1)}
    ba, bb = bigrams(a.lower()), bigrams(b.lower())
    if not ba and not bb:
        return 1.0
    return len(ba & bb) / len(ba | bb)


def _cosine(a: list[float], b: list[float]) -> float:
    dot  = sum(x * y for x, y in zip(a, b))
    na   = math.sqrt(sum(x * x for x in a))
    nb   = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if (na and nb) else 0.0


def _euclidean(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def similarity(text_a: str, text_b: str) -> dict[str, float]:
    """Return {cosine, euclidean, jaccard} scores between two texts.

    cosine and euclidean use Ollama embeddings when available; fall back to jaccard.
    """
    embeds = _embed_via_ollama([text_a, text_b])
    if embeds and len(embeds) == 2:
        cos = _cosine(embeds[0], embeds[1])
        euc = _euclidean(embeds[0], embeds[1])
        jac = _jaccard(text_a, text_b)
        return {"cosine": cos, "euclidean": euc, "jaccard": jac}

    jac = _jaccard(text_a, text_b)
    return {"cosine": jac, "euclidean": 0.0, "jaccard": jac}


def find_near_duplicates(
    records: list[dict[str, Any]],
    field: str = "code",
    threshold: float = _DEFAULT_DEDUP_THRESHOLD,
) -> list[tuple[int, int, float]]:
    """Return list of (idx_a, idx_b, cosine_score) for near-duplicate pairs.

    Only checks the specified field. Records without that field are skipped.
    Uses batch embedding for efficiency; falls back to pairwise Jaccard.
    """
    texts = [str(r.get(field, "")) for r in records]
    non_empty = [(i, t) for i, t in enumerate(texts) if t.strip()]

    if not non_empty:
        return []

    indices, batch = zip(*non_empty)
    embeds = _embed_via_ollama(list(batch))

    duplicates: list[tuple[int, int, float]] = []

    if embeds:
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                score = _cosine(embeds[i], embeds[j])
                if score >= threshold:
                    duplicates.append((indices[i], indices[j], score))
    else:
        # O(n²) Jaccard fallback — acceptable for small batches
        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                score = _jaccard(batch[i], batch[j])
                if score >= threshold:
                    duplicates.append((indices[i], indices[j], score))

    return duplicates


def filter_near_duplicates(
    input_path: Path | str,
    output_path: Path | str | None = None,
    field: str = "code",
    threshold: float = _DEFAULT_DEDUP_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter near-duplicates from a JSONL file. Return (unique, duplicates)."""
    records = read_jsonl(input_path)
    dup_pairs = find_near_duplicates(records, field, threshold)

    # Keep the first of each duplicate pair
    dup_indices: set[int] = {b for _, b, _ in dup_pairs}

    unique     = [r for i, r in enumerate(records) if i not in dup_indices]
    duplicated = [records[i] for i in dup_indices]

    logger.info(
        "Near-duplicate filter (field=%s, threshold=%.2f): %d unique / %d removed",
        field, threshold, len(unique), len(duplicated),
    )

    if output_path and unique:
        write_jsonl(output_path, unique)

    return unique, duplicated


def main() -> None:
    parser = argparse.ArgumentParser(description="Similarity check / near-duplicate detection")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input",  help="JSONL file for dedup mode")
    group.add_argument("--text1",  help="First text (pairwise mode)")
    parser.add_argument("--text2",     help="Second text (required with --text1)")
    parser.add_argument("--output",    help="Output JSONL (unique records only)")
    parser.add_argument("--field",     default="code", help="Field to embed for dedup")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_DEDUP_THRESHOLD)
    args = parser.parse_args()

    if args.text1:
        if not args.text2:
            parser.error("--text2 required with --text1")
        scores = similarity(args.text1, args.text2)
        print(f"cosine={scores['cosine']:.4f}  euclidean={scores['euclidean']:.4f}  jaccard={scores['jaccard']:.4f}")
    else:
        unique, dupes = filter_near_duplicates(args.input, args.output, args.field, args.threshold)
        if not args.output:
            print(f"Unique: {len(unique)}  Duplicates removed: {len(dupes)}")


if __name__ == "__main__":
    main()
