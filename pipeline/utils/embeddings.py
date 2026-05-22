"""Shared embedding backend with automatic fallback chain.

Priority order (first available wins):
  1. AWS Bedrock — amazon.titan-embed-text-v2:0  (passes ZScaler, same creds as advisor)
  2. Ollama — nomic-embed-text                   (local, blocked by ZScaler on corp network)
  3. sentence-transformers — nomic-embed-text-v1.5 (local, needs pip install)
  4. scikit-learn TF-IDF                          (always available, lower quality)

All functions return list[list[float]] normalised to unit norm.
Dimension: 1024 (Bedrock Titan v2) | 768 (nomic / sentence-transformers) | variable (TF-IDF)

Usage:
    from pipeline.utils.embeddings import embed, embed_dim

    vecs = embed(["text 1", "text 2"])
"""

from __future__ import annotations

import math
import os
from typing import Any

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.embeddings")

_BEDROCK_MODEL   = "amazon.titan-embed-text-v2:0"
_OLLAMA_MODEL    = "nomic-embed-text"
_OLLAMA_URL      = "http://localhost:11434"
_PROBE_TIMEOUT   = 5   # seconds — fail fast if backend unavailable
_BATCH_TIMEOUT   = 120 # seconds per batch call

# Resolved once per process
_BACKEND: str = ""   # "" = not yet probed


def _normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else vec


# ── Backend implementations ────────────────────────────────────────────────────

_BEDROCK_MAX_WORKERS = 20  # concurrent Bedrock calls (Titan v2 has no native batch API)


def _embed_bedrock(texts: list[str]) -> list[list[float]]:
    """Amazon Titan Text Embeddings v2 via boto3 Bedrock Runtime (parallelised)."""
    import boto3  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    def _single(idx_text: tuple[int, str]) -> tuple[int, list[float]]:
        idx, text = idx_text
        client = boto3.client("bedrock-runtime", region_name=region)
        resp = client.invoke_model(
            modelId=_BEDROCK_MODEL,
            body=_json.dumps({"inputText": text[:8000], "dimensions": 1024, "normalize": True}),
            contentType="application/json",
            accept="application/json",
        )
        body = _json.loads(resp["body"].read())
        return idx, body["embedding"]

    results: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
    workers = min(_BEDROCK_MAX_WORKERS, len(texts))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_single, (i, t)): i for i, t in enumerate(texts)}
        for fut in as_completed(futures):
            idx, vec = fut.result()
            results[idx] = vec
    return results


def _embed_ollama(texts: list[str]) -> list[list[float]]:
    import httpx  # noqa: PLC0415
    # Fast probe — raises immediately if model not loaded
    with httpx.Client(timeout=_PROBE_TIMEOUT) as probe:
        r = probe.post(f"{_OLLAMA_URL}/api/embeddings", json={"model": _OLLAMA_MODEL, "prompt": "probe"})
        r.raise_for_status()

    vecs = []
    with httpx.Client(timeout=_BATCH_TIMEOUT) as client:
        for text in texts:
            r = client.post(f"{_OLLAMA_URL}/api/embeddings", json={"model": _OLLAMA_MODEL, "prompt": text[:2048]})
            r.raise_for_status()
            vecs.append(r.json()["embedding"])
    return vecs


def _embed_sentence_transformers(texts: list[str]) -> list[list[float]]:
    import subprocess, sys, json as _json  # noqa: PLC0415,E401
    import concurrent.futures  # noqa: PLC0415

    script = (
        "import json,sys\n"
        "from sentence_transformers import SentenceTransformer\n"
        "texts=json.loads(sys.argv[1])\n"
        "m=SentenceTransformer('nomic-ai/nomic-embed-text-v1.5',trust_remote_code=True)\n"
        "print(json.dumps(m.encode(texts,normalize_embeddings=True).tolist()))\n"
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(
            subprocess.run,
            [sys.executable, "-c", script, _json.dumps(texts)],
            capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ, "TOKENIZERS_PARALLELISM": "false"},
        )
        proc = fut.result(timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(f"sentence-transformers failed: {proc.stderr[:200]}")
    return _json.loads(proc.stdout)


def _embed_tfidf(texts: list[str]) -> list[list[float]]:
    from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: PLC0415
    logger.warning("Using TF-IDF embeddings — cross-language similarity will be weak")
    vec = TfidfVectorizer(max_features=512, sublinear_tf=True)
    mat = vec.fit_transform(texts).toarray()
    return [_normalise(row.tolist()) for row in mat]


# ── Public API ─────────────────────────────────────────────────────────────────

def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using the best available backend. Never raises — falls back to TF-IDF."""
    global _BACKEND  # noqa: PLW0603

    if not texts:
        return []

    backends: list[tuple[str, Any]] = [
        ("bedrock",               _embed_bedrock),
        ("ollama",                _embed_ollama),
        ("sentence-transformers", _embed_sentence_transformers),
        ("tfidf",                 _embed_tfidf),
    ]

    # If a backend was already confirmed working, start from it
    start_idx = 0
    if _BACKEND:
        for i, (name, _) in enumerate(backends):
            if name == _BACKEND:
                start_idx = i
                break

    for name, fn in backends[start_idx:]:
        try:
            vecs = fn(texts)
            if _BACKEND != name:
                logger.info("Embedding backend: %s (dim=%d)", name, len(vecs[0]) if vecs else 0)
                _BACKEND = name
            return vecs
        except Exception as exc:  # noqa: BLE001
            logger.warning("Backend '%s' unavailable: %s — trying next", name, exc)
            if _BACKEND == name:
                _BACKEND = ""  # reset so next call re-probes from scratch

    # Should never reach here (tfidf always works if sklearn is installed)
    logger.error("All embedding backends failed — returning zero vectors")
    dim = 512
    return [[0.0] * dim for _ in texts]


def embed_dim() -> int:
    """Return the dimension of the current backend (probe with a single text)."""
    vecs = embed(["probe"])
    return len(vecs[0]) if vecs else 768


def current_backend() -> str:
    """Return the currently active embedding backend name (empty string if not yet probed)."""
    return _BACKEND


def reset_backend() -> None:
    """Force re-detection of the best backend on next call."""
    global _BACKEND  # noqa: PLW0603
    _BACKEND = ""
