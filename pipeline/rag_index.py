"""LanceDB RAG index — training-time pattern retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import yaml

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.rag_index")

_DEFAULT_DB_BASE = Path("lancedb")
_SEEDS_INDEX     = Path("config/rag_seeds/index.yaml")
_EMBED_DIM       = 768  # nomic-embed-text v1.5

# Singleton: loaded once, reused across all calls
_ST_MODEL = None


def _st_embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using sentence-transformers (local, no network after first download)."""
    global _ST_MODEL  # noqa: PLW0603
    if _ST_MODEL is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        logger.info("Loading nomic-embed-text-v1.5 (first call only)…")
        _ST_MODEL = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5", trust_remote_code=True)
    vecs = _ST_MODEL.encode(texts, normalize_embeddings=True)
    return vecs.tolist()


def _dummy_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic fallback for CI / offline environments."""
    rng = np.random.default_rng(42)
    return [rng.random(_EMBED_DIM).tolist() for _ in texts]


def _embed(texts: list[str]) -> list[list[float]]:
    try:
        return _st_embed(texts)
    except Exception as exc:
        logger.warning("sentence-transformers unavailable (%s) — using dummy embeddings", exc)
        return _dummy_embed(texts)


# ── Index ─────────────────────────────────────────────────────────────────────

class RagIndex:
    """
    LanceDB-backed index for training-time RAG retrieval.

    Each project gets its own LanceDB directory under lancedb/<project>/.
    The index aggregates patterns from:
      - config/rag_seeds/<domain>/    (domain idioms)
      - datalake/projects/<project>/raw/ (project-specific .json seeds)
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

        return len(records)

    def search(
        self,
        query: str,
        k: int = 3,
        domain_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return top-k patterns for a query. Optionally filter by domain."""
        if self._table is None:
            return []

        vec = _embed([query])[0]
        search = self._table.search(vec).limit(k)

        if domain_filter:
            search = search.where(f"domain = '{domain_filter}'")

        return search.select(["content", "domain", "category", "quality_label"]).to_list()

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
    """Return seed file paths for a domain from config/rag_seeds/index.yaml."""
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
        logger.info("Ingesting project seed %s", seed_path)
        total += idx.ingest_seeds(seed_path)

    logger.info("RAG index '%s': %d patterns total", project, total)
    return idx
