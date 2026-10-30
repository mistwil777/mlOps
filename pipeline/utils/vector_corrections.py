"""Qdrant vector store for semantic few-shot correction retrieval.

Collection: corrections_validated
Each correction is embedded as: "phase | description | observed | expected"
At inference: find top-K similar corrections to inject as few-shot examples.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

_COLLECTION = "corrections_validated"
_EMBED_DIM = 768  # nomic-embed-text v1.5
_OLLAMA_BASE = "http://localhost:11434"


def _qdrant():
    from qdrant_client import QdrantClient
    return QdrantClient(url="http://localhost:6333")


def _embed(text: str) -> list[float] | None:
    """Embed text via nomic-embed-text through Ollama."""
    try:
        import httpx
        r = httpx.post(
            f"{_OLLAMA_BASE}/api/embed",
            json={"model": "nomic-embed-text:latest", "input": text},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        embeddings = data.get("embeddings") or data.get("embedding")
        if isinstance(embeddings, list) and embeddings:
            vec = embeddings[0] if isinstance(embeddings[0], list) else embeddings
            return vec
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
    return None


def _ensure_collection() -> bool:
    try:
        client = _qdrant()
        existing = {c.name for c in client.get_collections().collections}
        if _COLLECTION not in existing:
            from qdrant_client.models import Distance, VectorParams
            client.create_collection(
                _COLLECTION,
                vectors_config=VectorParams(size=_EMBED_DIM, distance=Distance.COSINE),
            )
        return True
    except Exception as e:
        logger.warning("Qdrant not available: %s", e)
        return False


def upsert_correction(correction: dict) -> bool:
    """Index a validated correction for semantic retrieval."""
    if not _ensure_collection():
        return False
    text = " | ".join(filter(None, [
        correction.get("phase", ""),
        correction.get("description", ""),
        correction.get("observed_value", ""),
        correction.get("expected_value", ""),
    ]))
    vec = _embed(text)
    if vec is None:
        return False
    try:
        from qdrant_client.models import PointStruct
        point_id = int(hashlib.md5(correction["id"].encode()).hexdigest()[:8], 16)
        _qdrant().upsert(
            collection_name=_COLLECTION,
            points=[PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "correction_id": correction["id"],
                    "phase": correction.get("phase", ""),
                    "description": correction.get("description", ""),
                    "observed_value": correction.get("observed_value", ""),
                    "expected_value": correction.get("expected_value", ""),
                    "rationale": correction.get("rationale", ""),
                    "confidence": correction.get("confidence", 0.5),
                },
            )],
        )
        return True
    except Exception as e:
        logger.warning("Qdrant upsert failed: %s", e)
        return False


def search_similar_corrections(query: str, phase: str, top_k: int = 5) -> list[dict]:
    """Find semantically similar validated corrections for few-shot injection."""
    if not _ensure_collection():
        return []
    vec = _embed(query)
    if vec is None:
        return []
    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue
        results = _qdrant().search(
            collection_name=_COLLECTION,
            query_vector=vec,
            query_filter=Filter(must=[FieldCondition(key="phase", match=MatchValue(value=phase))]),
            limit=top_k,
            score_threshold=0.5,
        )
        return [
            {**hit.payload, "score": hit.score}
            for hit in results
        ]
    except Exception as e:
        logger.warning("Qdrant search failed: %s", e)
        return []


def delete_correction(correction_id: str) -> bool:
    """Remove a correction from the vector index (e.g. after rejection)."""
    try:
        point_id = int(hashlib.md5(correction_id.encode()).hexdigest()[:8], 16)
        _qdrant().delete(collection_name=_COLLECTION, points_selector=[point_id])
        return True
    except Exception as e:
        logger.warning("Qdrant delete failed: %s", e)
        return False


def is_available() -> bool:
    try:
        _qdrant().get_collections()
        return True
    except Exception:
        return False
