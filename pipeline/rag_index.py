"""LanceDB RAG index — hybrid (vector + full-text) pattern retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import lancedb
import numpy as np
import yaml

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.rag_index")

_DEFAULT_DB_BASE = Path("lancedb")
_SEEDS_INDEX     = Path("config/rag_seeds/index.yaml")
_EMBED_DIM       = 768  # nomic-embed-text v1.5

_ST_MODEL = None


def _st_embed(texts: list[str]) -> list[list[float]]:
    global _ST_MODEL  # noqa: PLW0603
    if _ST_MODEL is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        logger.info("Loading nomic-embed-text-v1.5 (first call only)…")
        _ST_MODEL = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5", trust_remote_code=True)
    vecs = _ST_MODEL.encode(texts, normalize_embeddings=True)
    return vecs.tolist()


def _dummy_embed(texts: list[str]) -> list[list[float]]:
    rng = np.random.default_rng(42)
    return [rng.random(_EMBED_DIM).tolist() for _ in texts]


def _embed(texts: list[str]) -> list[list[float]]:
    try:
        return _st_embed(texts)
    except Exception as exc:
        logger.warning("sentence-transformers unavailable (%s) — using dummy embeddings", exc)
        return _dummy_embed(texts)


def _rrf(
    vec_results: list[dict[str, Any]],
    fts_results: list[dict[str, Any]],
    k: int = 3,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion — merge vector and full-text ranked lists."""
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


# ── Index ─────────────────────────────────────────────────────────────────────

SearchMode = Literal["vector", "fulltext", "hybrid"]


class RagIndex:
    """
    LanceDB-backed index for training-time RAG retrieval.

    Each project gets its own LanceDB directory under lancedb/<project>/.
    Supports three search modes:
      - "vector"    : semantic similarity (cosine distance on embeddings)
      - "fulltext"  : BM25 keyword search (exact terms, identifiers, APIs)
      - "hybrid"    : RRF fusion of both (default — best of both worlds)

    The index aggregates patterns from:
      - config/rag_seeds/<domain>/       (domain idioms)
      - datalake/projects/<project>/raw/ (project-specific .json seeds)
      - datalake/projects/<project>/raw/feedback/ (ground feedback from sol teams)
    """

    def __init__(self, project: str, db_base: Path = _DEFAULT_DB_BASE):
        self._db_path = db_base / project
        self._db = lancedb.connect(str(self._db_path))
        self._table_name = "patterns"
        self._table = self._open_or_none()

    def _open_or_none(self):
        try:
            return self._db.open_table(self._table_name)
        except Exception:
            return None

    def _ensure_fts_index(self) -> None:
        if self._table is None:
            return
        try:
            self._table.create_fts_index("content", replace=False)
        except Exception:
            pass  # index already exists or FTS unavailable

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

        records = [
            {
                "vector":        vec,
                "content":       p["content"],
                "domain":        data.get("domain", data.get("lang", "")),
                "category":      p["metadata"].get("category", ""),
                "quality_label": p["metadata"].get("quality_label", ""),
                "source_file":   str(seeds_path.name),
            }
            for p, vec in zip(patterns, vectors)
        ]

        if self._table is None:
            self._table = self._db.create_table(self._table_name, data=records)
            logger.info("Created '%s' with %d records", self._table_name, len(records))
        else:
            self._table.add(records)
            logger.info("Added %d records to '%s'", len(records), self._table_name)

        self._ensure_fts_index()
        return len(records)

    def ingest_feedback(self, entry: dict[str, Any]) -> int:
        """Ingest a single validated feedback entry from sol teams."""
        content = entry.get("content", "").strip()
        if not content:
            return 0

        vec = _embed([content])[0]
        record = {
            "vector":        vec,
            "content":       content,
            "domain":        entry.get("domain", ""),
            "category":      entry.get("category", "feedback"),
            "quality_label": entry.get("quality_label", "validated"),
            "source_file":   "feedback",
        }

        if self._table is None:
            self._table = self._db.create_table(self._table_name, data=[record])
        else:
            self._table.add([record])

        self._ensure_fts_index()
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
        if self._table is None:
            return []

        fetch_k = k * 4  # over-fetch before RRF pruning

        def _apply_filter(search_obj: Any) -> Any:
            if domain_filter:
                return search_obj.where(f"domain = '{domain_filter}'")
            return search_obj

        cols = ["content", "domain", "category", "quality_label"]

        if mode == "vector":
            vec = _embed([query])[0]
            q = _apply_filter(self._table.search(vec).limit(k))
            return q.select(cols).to_list()

        if mode == "fulltext":
            try:
                q = _apply_filter(
                    self._table.search(query, query_type="fts").limit(k)
                )
                return q.select(cols).to_list()
            except Exception as exc:
                logger.warning("FTS search failed (%s) — falling back to vector", exc)
                vec = _embed([query])[0]
                return _apply_filter(self._table.search(vec).limit(k)).select(cols).to_list()

        # hybrid: RRF fusion
        vec = _embed([query])[0]
        vec_results: list[dict[str, Any]] = []
        fts_results: list[dict[str, Any]] = []

        try:
            vec_results = (
                _apply_filter(self._table.search(vec).limit(fetch_k))
                .select(cols)
                .to_list()
            )
        except Exception as exc:
            logger.warning("Vector search failed (%s)", exc)

        try:
            fts_results = (
                _apply_filter(self._table.search(query, query_type="fts").limit(fetch_k))
                .select(cols)
                .to_list()
            )
        except Exception as exc:
            logger.warning("FTS search failed (%s) — hybrid degraded to vector only", exc)

        if not fts_results:
            return vec_results[:k]
        if not vec_results:
            return fts_results[:k]

        return _rrf(vec_results, fts_results, k=k)

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
        if self._table is None:
            return 0
        return self._table.count_rows()

    def drop(self) -> None:
        try:
            self._db.drop_table(self._table_name)
        except Exception:
            pass
        self._table = None


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
) -> RagIndex:
    """Build (or reuse) a RAG index for a project domain."""
    idx = RagIndex(project)

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
