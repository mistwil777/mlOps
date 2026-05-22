"""Qdrant RAG index — hybrid (vector + full-text) pattern retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import numpy as np
import yaml
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

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.rag_index")

_SEEDS_INDEX = Path("config/rag_seeds/index.yaml")
_EMBED_DIM   = None  # resolved lazily from active backend (1024 Bedrock / 768 nomic)
_COLLECTION  = "patterns"

from pipeline.utils.embeddings import embed as _embed_texts, embed_dim as _embed_dim_fn  # noqa: E402


def _get_embed_dim() -> int:
    global _EMBED_DIM  # noqa: PLW0603
    if _EMBED_DIM is None:
        _EMBED_DIM = _embed_dim_fn()
    return _EMBED_DIM


def _embed(texts: list[str]) -> list[list[float]]:
    return _embed_texts(texts)


def _dummy_embed(texts: list[str]) -> list[list[float]]:
    rng = np.random.default_rng(42)
    return [rng.random(_get_embed_dim()).tolist() for _ in texts]


def _qdrant_client(host: str = "localhost", port: int = 6333) -> QdrantClient:
    return QdrantClient(host=host, port=port)


# ── Index ─────────────────────────────────────────────────────────────────────

SearchMode = Literal["vector", "fulltext", "hybrid"]


class RagIndex:
    """
    Qdrant-backed index for RAG retrieval — used both at training time and in production.

    Each project gets its own Qdrant collection: patterns_<project_id>.
    Supports three search modes:
      - "vector"   : semantic similarity (cosine distance on embeddings)
      - "fulltext" : keyword filter on payload fields
      - "hybrid"   : RRF fusion of both (default)

    The index aggregates patterns from:
      - config/rag_seeds/<domain>/       (domain idioms)
      - datalake/projects/<project>/raw/ (project-specific .json seeds)
      - datalake/projects/<project>/raw/feedback/ (validated feedback)
    """

    def __init__(self, project: str, host: str = "localhost", port: int = 6333):
        self._project    = project
        self._collection = f"patterns_{project}"
        self._client     = _qdrant_client(host, port)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=_get_embed_dim(), distance=Distance.COSINE),
            )
            self._client.create_payload_index(
                collection_name=self._collection,
                field_name="content",
                field_schema=PayloadSchemaType.TEXT,
            )
            logger.info("Created Qdrant collection '%s'", self._collection)

    def ingest_seeds(self, seeds_path: Path) -> int:
        """Ingest a RAG seed JSON file. Returns number of records inserted."""
        with seeds_path.open(encoding="utf-8") as fh:
            data = json.load(fh)

        patterns = data.get("patterns", [])
        if not patterns:
            logger.warning("No patterns found in %s", seeds_path)
            return 0

        texts   = [p["content"] for p in patterns]
        vectors = _embed(texts)

        points = [
            PointStruct(
                id=abs(hash(p["content"])) % (2**63),
                vector=vec,
                payload={
                    "content":       p["content"],
                    "domain":        data.get("domain", data.get("lang", "")),
                    "category":      p["metadata"].get("category", ""),
                    "quality_label": p["metadata"].get("quality_label", ""),
                    "source_file":   str(seeds_path.name),
                },
            )
            for p, vec in zip(patterns, vectors)
        ]

        self._client.upsert(collection_name=self._collection, points=points)
        logger.info("Upserted %d records into '%s'", len(points), self._collection)
        return len(points)

    def ingest_feedback(self, entry: dict[str, Any]) -> int:
        """Ingest a single validated feedback entry."""
        content = entry.get("content", "").strip()
        if not content:
            return 0

        vec = _embed([content])[0]
        point = PointStruct(
            id=abs(hash(content)) % (2**63),
            vector=vec,
            payload={
                "content":       content,
                "domain":        entry.get("domain", ""),
                "category":      entry.get("category", "feedback"),
                "quality_label": entry.get("quality_label", "validated"),
                "source_file":   "feedback",
            },
        )
        self._client.upsert(collection_name=self._collection, points=[point])
        logger.info("Ingested feedback entry (domain=%s)", entry.get("domain", "?"))
        return 1

    def search(
        self,
        query: str,
        k: int = 3,
        domain_filter: str | None = None,
        mode: SearchMode = "hybrid",
    ) -> list[dict[str, Any]]:
        """Return top-k patterns. mode: 'vector' | 'fulltext' | 'hybrid'."""
        qfilter: Filter | None = None
        if domain_filter:
            qfilter = Filter(
                must=[FieldCondition(key="domain", match=MatchValue(value=domain_filter))]
            )

        vec = _embed([query])[0]

        if mode in ("vector", "hybrid"):
            vec_hits = self._client.search(
                collection_name=self._collection,
                query_vector=vec,
                query_filter=qfilter,
                limit=k * 4 if mode == "hybrid" else k,
                with_payload=True,
            )
            vec_results = [h.payload for h in vec_hits]
        else:
            vec_results = []

        if mode in ("fulltext", "hybrid"):
            # MatchText does token-level substring matching (requires a text index on content)
            must = [FieldCondition(key="content", match=MatchText(text=query))]
            if domain_filter:
                must.append(FieldCondition(key="domain", match=MatchValue(value=domain_filter)))
            scroll_filter = Filter(must=must)
            scroll_hits, _ = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=scroll_filter,
                limit=k * 4 if mode == "hybrid" else k,
                with_payload=True,
            )
            fts_results = [h.payload for h in scroll_hits]
        else:
            fts_results = []

        if mode == "hybrid":
            return _rrf(vec_results, fts_results, k=k)
        return (vec_results or fts_results)[:k]

    def format_context(self, results: list[dict[str, Any]]) -> str:
        """Serialize search results into the rag_context field of a JSONL pair."""
        if not results:
            return ""
        return "\n\n".join(
            f"[{r.get('domain', '?')} / {r.get('category', '?')}] {r['content']}"
            for r in results
        )

    @property
    def count(self) -> int:
        info = self._client.get_collection(self._collection)
        return info.points_count or 0

    def drop(self) -> None:
        self._client.delete_collection(self._collection)
        logger.info("Dropped Qdrant collection '%s'", self._collection)
        self._ensure_collection()


def _rrf(
    vec_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    k: int = 3,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    items:  dict[str, dict[str, Any]] = {}

    for rank, r in enumerate(vec_results):
        key = r["content"]
        scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
        items[key] = r

    for rank, r in enumerate(fts_results):
        key = r["content"]
        scores[key] = scores.get(key, 0.0) + 1.0 / (rrf_k + rank + 1)
        items[key] = r

    ranked = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [items[key] for key in ranked[:k]]


# ── Build helpers ─────────────────────────────────────────────────────────────

def _seeds_for_domain(domain: str) -> list[Path]:
    if not _SEEDS_INDEX.exists():
        return list(Path(f"config/rag_seeds/{domain}").glob("*.json"))

    with _SEEDS_INDEX.open() as fh:
        index = yaml.safe_load(fh)

    entry = index.get("domains", index.get("languages", {})).get(domain, {})
    return [Path(p) for p in entry.get("seeds", [])]


def build_index(
    project: str,
    domain: str,
    rebuild: bool = False,
    host: str = "localhost",
    port: int = 6333,
) -> RagIndex:
    """Build (or reuse) a RAG index for a project domain."""
    idx = RagIndex(project, host=host, port=port)

    if rebuild:
        idx.drop()

    if idx.count > 0 and not rebuild:
        logger.info("RAG index '%s' already has %d patterns — skipping ingest", project, idx.count)
        return idx

    total = 0
    for seed_path in _seeds_for_domain(domain):
        if seed_path.exists():
            logger.info("Ingesting %s", seed_path)
            total += idx.ingest_seeds(seed_path)
        else:
            logger.warning("Seed file not found: %s", seed_path)

    project_seeds = Path(f"datalake/projects/{project}/raw")
    for seed_path in project_seeds.glob("*.json"):
        if seed_path.parent.name != "feedback":
            logger.info("Ingesting project seed %s", seed_path)
            total += idx.ingest_seeds(seed_path)

    logger.info("RAG index '%s': %d patterns total", project, total)
    return idx
