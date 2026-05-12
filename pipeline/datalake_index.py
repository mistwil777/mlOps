"""Global datalake index — semantic search across all project training pairs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchText,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from pipeline.rag_index import _embed, _rrf
from pipeline.utils.jsonl import read_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.datalake_index")

_COLLECTION = "datalake_pairs"
_EMBED_DIM  = 768
_BATCH_SIZE = 100
_DATALAKE   = Path("datalake/projects")


def _pair_hash(prompt: str, completion: str) -> str:
    return hashlib.md5((prompt + completion).encode()).hexdigest()


def _pair_id(pair_hash: str) -> int:
    return int(pair_hash, 16) % (2**63)


class DatalakeIndex:
    """
    Qdrant-backed global index of training pairs across all projects.
    Collection: datalake_pairs (single shared collection, project_id in payload).
    Supports hybrid search (vector + fulltext RRF) and optional project/stage filters.
    """

    def __init__(self, host: str = "localhost", port: int = 6333):
        self._client = QdrantClient(host=host, port=port)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if _COLLECTION in existing:
            return

        self._client.create_collection(
            collection_name=_COLLECTION,
            vectors_config=VectorParams(size=_EMBED_DIM, distance=Distance.COSINE),
        )
        for field, schema in (
            ("prompt",     PayloadSchemaType.TEXT),
            ("completion", PayloadSchemaType.TEXT),
            ("project_id", PayloadSchemaType.KEYWORD),
            ("stage",      PayloadSchemaType.KEYWORD),
        ):
            self._client.create_payload_index(
                collection_name=_COLLECTION,
                field_name=field,
                field_schema=schema,
            )
        logger.info("Created Qdrant collection '%s'", _COLLECTION)

    def index_project(
        self,
        project_id: str,
        stage: str = "fine-tuning-ready",
    ) -> int:
        """
        Index all JSONL pairs from datalake/projects/{project_id}/{stage}/.
        Deduplicates via pair_hash — safe to call repeatedly (upsert).
        Returns number of points upserted.
        """
        stage_dir = _DATALAKE / project_id / stage
        if not stage_dir.exists():
            logger.warning("Stage dir not found: %s", stage_dir)
            return 0

        jsonl_files = list(stage_dir.glob("*.jsonl"))
        if not jsonl_files:
            logger.warning("No JSONL files in %s", stage_dir)
            return 0

        all_pairs: list[dict[str, Any]] = []
        for f in jsonl_files:
            all_pairs.extend(read_jsonl(f))

        if not all_pairs:
            return 0

        total = 0
        batch: list[dict[str, Any]] = []
        texts: list[str] = []

        for pair in all_pairs:
            prompt     = str(pair.get("prompt", ""))[:512]
            completion = str(pair.get("completion", ""))[:512]
            if not prompt and not completion:
                continue
            batch.append(pair)
            texts.append(f"{prompt[:400]} → {completion[:400]}")

            if len(batch) >= _BATCH_SIZE:
                total += self._upsert_batch(batch, texts, project_id, stage)
                batch, texts = [], []

        if batch:
            total += self._upsert_batch(batch, texts, project_id, stage)

        logger.info("Indexed %d pairs for project=%s stage=%s", total, project_id, stage)
        return total

    def _upsert_batch(
        self,
        pairs: list[dict[str, Any]],
        texts: list[str],
        project_id: str,
        stage: str,
    ) -> int:
        vectors = _embed(texts)
        points = []
        for pair, vec in zip(pairs, vectors):
            prompt     = str(pair.get("prompt", ""))[:512]
            completion = str(pair.get("completion", ""))[:512]
            ph = _pair_hash(prompt, completion)
            points.append(
                PointStruct(
                    id=_pair_id(ph),
                    vector=vec,
                    payload={
                        "project_id":    project_id,
                        "stage":         stage,
                        "prompt":        prompt,
                        "completion":    completion,
                        "quality_score": pair.get("quality_score"),
                        "collected_at":  pair.get("collected_at"),
                        "source_url":    pair.get("source_url"),
                        "pair_hash":     ph,
                    },
                )
            )
        self._client.upsert(collection_name=_COLLECTION, points=points)
        return len(points)

    def search(
        self,
        query: str,
        *,
        project_id: str | None = None,
        stage: str | None = None,
        k: int = 20,
    ) -> list[dict[str, Any]]:
        """
        Hybrid semantic + fulltext search over datalake_pairs.
        Optional filters on project_id and/or stage.
        Returns list of payload dicts.
        """
        must: list[FieldCondition] = []
        if project_id:
            must.append(FieldCondition(key="project_id", match=MatchValue(value=project_id)))
        if stage:
            must.append(FieldCondition(key="stage", match=MatchValue(value=stage)))

        qfilter: Filter | None = Filter(must=must) if must else None
        vec = _embed([query])[0]

        vec_hits = self._client.search(
            collection_name=_COLLECTION,
            query_vector=vec,
            query_filter=qfilter,
            limit=k * 4,
            with_payload=True,
        )
        vec_results = [h.payload for h in vec_hits]

        fts_must = [FieldCondition(key="prompt", match=MatchText(text=query))]
        if must:
            fts_must.extend(must)
        scroll_hits, _ = self._client.scroll(
            collection_name=_COLLECTION,
            scroll_filter=Filter(must=fts_must),
            limit=k * 4,
            with_payload=True,
        )
        fts_results = [h.payload for h in scroll_hits]

        return _rrf_pairs(vec_results, fts_results, k=k)

    def match_adapter(self, adapter_desc: str, k: int = 10) -> list[dict[str, Any]]:
        """Semantic match against adapter description — no project filter."""
        return self.search(adapter_desc, k=k)

    @property
    def count(self) -> int:
        try:
            info = self._client.get_collection(_COLLECTION)
            return info.points_count or 0
        except Exception:
            return 0


def _rrf_pairs(
    vec_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    k: int = 20,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """RRF fusion keyed on pair_hash (not content, to handle truncation)."""
    scores: dict[str, float] = {}
    items:  dict[str, dict[str, Any]] = {}

    for rank, r in enumerate(vec_results):
        key = r.get("pair_hash") or r.get("prompt", "")[:80]
        scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
        items[key] = r

    for rank, r in enumerate(fts_results):
        key = r.get("pair_hash") or r.get("prompt", "")[:80]
        scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
        items[key] = r

    ranked = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [items[key] for key in ranked[:k]]
