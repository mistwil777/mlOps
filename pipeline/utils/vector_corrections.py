"""Qdrant vector store for semantic few-shot correction retrieval.

Collection: corrections_validated_{dim} (dim depends on active embedding backend)
Each correction is embedded as: "phase | description | observed | expected"
At inference: find top-K similar corrections to inject as few-shot examples.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _collection_name() -> str:
    from pipeline.utils.embeddings import embed_dim
    return f"corrections_validated_{embed_dim()}"


def _qdrant():
    from qdrant_client import QdrantClient
    return QdrantClient(url="http://localhost:6333")


def _embed(text: str) -> list[float] | None:
    try:
        from pipeline.utils.embeddings import embed
        vecs = embed([text])
        return vecs[0] if vecs else None
    except Exception as e:
        logger.warning("Embedding failed: %s", e)
    return None


def _ensure_collection() -> bool:
    try:
        from pipeline.utils.embeddings import embed_dim
        client = _qdrant()
        name = _collection_name()
        existing = {c.name for c in client.get_collections().collections}
        if name not in existing:
            from qdrant_client.models import Distance, VectorParams
            client.create_collection(
                name,
                vectors_config=VectorParams(size=embed_dim(), distance=Distance.COSINE),
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
        name = _collection_name()
        point_id = int(hashlib.md5(correction["id"].encode()).hexdigest()[:8], 16)
        _qdrant().upsert(
            collection_name=name,
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
            collection_name=_collection_name(),
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
        name = _collection_name()
        point_id = int(hashlib.md5(correction_id.encode()).hexdigest()[:8], 16)
        _qdrant().delete(collection_name=name, points_selector=[point_id])
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
