"""pair-forge — PairForgeAgent.

Orchestrates raw file ingestion into fine-tuning pairs:
  extract → clean → generate (LLM) → judge (LLM) → memory (Qdrant) → correct → save

Each LLM call is stateless — context is rebuilt from scratch at every iteration
using only the current task + relevant memories retrieved from Qdrant.

Designed for portability: LLM, embedding, and logging backends are injected
at construction time. Default implementations are provided but not required.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Generator
from pathlib import Path

from pair_forge.extractors import extract_content
from pair_forge.prompts import (
    CONTENT_EXTRACTION, JUDGE_EVALUATION, PAIR_CORRECTION, PAIR_GENERATION,
    _format_rag_context, _format_kg_context, _format_kg_constraints,
)
from pair_forge.tools import apply_tools, check_dependencies, detect_language

# ── Default backend implementations (lazy imports — only used if not injected) ─

def _default_llm(prompt: str, llm_config: dict, max_tokens: int = 2048) -> str | None:
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        model_id    = llm_config.get("model_id", "us.amazon.nova-pro-v1:0")
        temperature = float(llm_config.get("temperature", 0.1))
        region      = llm_config.get("region", "us-east-1")
        client = boto3.client(
            "bedrock-runtime", region_name=region,
            config=Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 2}),
        )
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return response["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("pair_forge").warning("Default LLM call failed: %s", exc)
        return None


def _default_embed(texts: list[str]) -> list[list[float]]:
    try:
        import boto3  # noqa: PLC0415

        client  = boto3.client("bedrock-runtime", region_name="us-east-1")
        results = []
        for text in texts:
            r = client.invoke_model(
                modelId="amazon.titan-embed-text-v2:0",
                body=json.dumps({"inputText": text, "dimensions": 1024}),
            )
            results.append(json.loads(r["body"].read())["embedding"])
        return results
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("pair_forge").warning("Default embed call failed: %s", exc)
        return []


def _default_log(level: str, message: str) -> None:
    getattr(logging.getLogger("pair_forge"), level, logging.getLogger("pair_forge").info)(message)


# ── Agent ──────────────────────────────────────────────────────────────────────

class PairForgeAgent:
    """Transforms raw documents into validated fine-tuning pairs.

    Args:
        project_id: Identifier used for Qdrant collection namespacing and output paths.
        config:     Project config dict. Expected keys:
                      ingestion.pair_generator, ingestion.pair_judge,
                      domain, task_type, adapters, data.raw,
                      min_tokens, max_tokens.
        llm_fn:     callable(prompt, llm_config, max_tokens) -> str | None
                    Defaults to AWS Bedrock via boto3.
        embed_fn:   callable(texts) -> list[list[float]]
                    Defaults to AWS Bedrock Titan v2.
        log_fn:     callable(level, message) -> None
                    Defaults to Python logging.
        qdrant_url: Qdrant base URL. Defaults to http://localhost:6333.
    """

    def __init__(
        self,
        project_id: str,
        config: dict,
        llm_fn:    Callable[[str, dict, int], str | None]   | None = None,
        embed_fn:  Callable[[list[str]], list[list[float]]] | None = None,
        log_fn:    Callable[[str, str], None]               | None = None,
        qdrant_url: str = "http://localhost:6333",
    ) -> None:
        self.project_id  = project_id
        self.config      = config
        self.ingest_cfg  = config.get("ingestion", {})
        self.gen_cfg     = self.ingest_cfg.get("pair_generator", {})
        self.judge_cfg   = self.ingest_cfg.get("pair_judge", {})
        self.min_score   = float(self.judge_cfg.get("min_score", 0.8))
        self.max_retries = int(self.judge_cfg.get("max_retries", 2))
        self.min_tokens  = int(config.get("min_tokens", 50))
        self.max_tokens  = int(config.get("max_tokens", 2048))
        self.qdrant_url  = qdrant_url
        self._llm        = llm_fn    or _default_llm
        self._embed      = embed_fn  or _default_embed
        self._log        = log_fn    or _default_log
        self._qdrant     = None
        self._rag_ctx:   object | None = None  # IngestionRagIndex — lazy init
        self._kg:        object | None = None  # ProjectKG — lazy init

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        file_path: Path,
        source: str,
        adapter_name: str | None = None,
    ) -> Generator[dict, None, None]:
        """Yield log dicts throughout processing. Final dict contains key 'summary'."""
        adapter = self._resolve_adapter(adapter_name)
        yield self._info(f"Starting: {file_path.name}")

        # --- Init RAG + KG semantic context ---
        self._init_semantic_context()
        yield self._info(
            f"Semantic context: RAG={self._rag_ctx_count()} chunks, "
            f"KG={'available' if self._kg_available() else 'unavailable'}"
        )

        # --- Dependency check ---
        dep_status = check_dependencies()
        for item in dep_status.get("missing", []):
            yield self._warn(f"Capability unavailable: {item['capability']} — {item['install_hint']}")
        for item in dep_status.get("deprecated", []):
            yield self._warn(f"Deprecated tool: {item['capability']} — replaced by: {item['replaced_by']}")
        for item in dep_status.get("untested_versions", []):
            yield self._warn(f"Untested version: {item['capability']} (tested up to {item['tested_up_to']})")

        # --- Extract content ---
        try:
            content, method = extract_content(file_path)
            yield self._info(f"Extracted {len(content)} chars via {method}")
        except ValueError as exc:
            yield self._error(str(exc))
            return

        if not content.strip():
            yield self._warn("Empty content after extraction")
            return

        # --- Clean and normalize ---
        content, tools_applied = apply_tools(content, method)
        if tools_applied:
            yield self._info(f"Tools applied: {', '.join(tools_applied)}")

        lang = detect_language(content)
        if lang != "unknown":
            yield self._info(f"Detected language: {lang}")

        if not content.strip():
            yield self._warn("Empty content after cleaning")
            return

        # --- Extract relevant snippets (light pre-filter) ---
        yield self._info("Extracting relevant content...")
        snippets = self._extract_relevant(content, adapter)
        if not snippets:
            yield self._warn("No relevant content found for this adapter — skipping pair generation")
            return
        yield self._info(f"{len(snippets)} snippet(s) extracted")
        filtered_content = "\n\n---\n\n".join(snippets)

        # --- Generate pairs ---
        yield self._info("Generating pairs...")
        pairs = self._generate_pairs(filtered_content, adapter)
        if not pairs:
            yield self._error("Pair generation returned no results")
            return
        yield self._info(f"{len(pairs)} pairs generated")

        # --- Judge and iterate ---
        validated, rejected = [], []
        for i, pair in enumerate(pairs):
            yield self._info(f"Judging pair {i + 1}/{len(pairs)}...")
            result = self._judge_and_iterate(pair, adapter)
            if result["status"] == "validated":
                validated.append(result["pair"])
                yield self._info(f"  + Pair {i + 1} validated (score {result['score']:.2f})")
            else:
                rejected.append(result)
                reason = result.get("error_type") or "unknown"
                desc   = result.get("error_description") or ""
                score  = result.get("score", 0.0) if "score" in result else "—"
                yield self._warn(
                    f"  - Pair {i + 1} rejected after {result['attempts']} attempts"
                    f" | score={score} | {reason}: {desc}"
                )

        # --- Save ---
        n_saved = self._save_pairs(validated, source, adapter)
        yield self._info(f"Saved {n_saved} pairs")

        yield {
            "summary":          True,
            "generated":        len(pairs),
            "validated":        len(validated),
            "rejected":         len(rejected),
            "saved":            n_saved,
            "patterns_learned": self._count_patterns(adapter),
            "method":           method,
        }

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def _extract_relevant(self, content: str, adapter: dict) -> list[str]:
        """Light pre-filter: extract useful snippets via a fast LLM call."""
        extract_cfg = {
            **self.gen_cfg,
            "model_id": self.gen_cfg.get("extract_model_id", "amazon.nova-lite-v1:0"),
            "temperature": 0.1,
        }
        prompt = CONTENT_EXTRACTION.format(
            adapter_name=adapter.get("name", "default"),
            adapter_description=adapter.get("description", ""),
            domain=self.config.get("domain", "general"),
            content=content[:12000],
        )
        raw = self._llm(prompt, extract_cfg, 2048)
        if not raw:
            return []
        result = self._parse_json_list(raw)
        return [s for s in result if isinstance(s, str) and s.strip()]

    def _generate_pairs(self, content: str, adapter: dict) -> list[dict]:
        n_pairs = max(1, min(20, len(content) // 500))

        # Retrieve semantic context
        rag_context = self._search_rag(content[:500]) if self._rag_ctx else ""
        kg_context  = self._search_kg(content[:500])  if self._kg    else ""

        prompt = PAIR_GENERATION.format(
            domain=self.config.get("domain", "general"),
            task_type=self.config.get("task_type", "generation"),
            adapter_name=adapter.get("name", "default"),
            adapter_description=adapter.get("description", ""),
            n_pairs=n_pairs,
            min_tokens=self.min_tokens,
            max_tokens=self.max_tokens,
            content=content[:8000],
            rag_context_block=_format_rag_context(rag_context),
            kg_context_block=_format_kg_context(kg_context),
        )
        raw = self._llm(prompt, self.gen_cfg, 4096)
        return self._parse_json_list(raw) if raw else []

    # ------------------------------------------------------------------
    # Judge + iterative correction
    # ------------------------------------------------------------------

    def _judge_and_iterate(self, pair: dict, adapter: dict) -> dict:
        attempts     = 0
        current_pair = pair
        error_type   = "unknown"
        error_desc   = ""

        while attempts <= self.max_retries:
            verdict  = self._judge_pair(current_pair, adapter)
            score    = verdict.get("score", 0.0)
            attempts += 1

            if score >= self.min_score:
                return {"status": "validated", "pair": current_pair, "score": score, "attempts": attempts}

            error_type = verdict.get("error_type") or "unknown"
            error_desc = verdict.get("error_description") or ""
            suggested  = verdict.get("suggested_fix") or ""

            self._store_memory(adapter, error_type, error_desc, suggested)

            if attempts > self.max_retries:
                break

            memories = self._retrieve_memories(adapter, error_desc)
            corrected = self._correct_pair(
                current_pair, error_type, error_desc, suggested,
                self._format_memories(memories),
            )
            if corrected:
                current_pair = corrected

        return {"status": "rejected", "pair": current_pair, "error_type": error_type,
                "error_description": error_desc, "score": score, "attempts": attempts}

    def _judge_pair(self, pair: dict, adapter: dict) -> dict:
        constraints = self._get_kg_constraints()
        prompt = JUDGE_EVALUATION.format(
            domain=self.config.get("domain", "general"),
            adapter_name=adapter.get("name", "default"),
            input=pair.get("input", ""),
            output=pair.get("output", ""),
            kg_constraints_block=_format_kg_constraints(constraints),
        )
        raw = self._llm(prompt, self.judge_cfg, 256)
        return self._parse_json_object(raw) or {"score": 0.0}

    def _correct_pair(self, pair: dict, error_type: str, error_desc: str,
                      suggested: str, memory_context: str) -> dict | None:
        prompt = PAIR_CORRECTION.format(
            input=pair.get("input", ""),
            output=pair.get("output", ""),
            error_type=error_type,
            error_description=error_desc,
            suggested_fix=suggested,
            memory_context=memory_context or "No similar past errors found.",
        )
        raw = self._llm(prompt, self.gen_cfg, 2048)
        return self._parse_json_object(raw) if raw else None

    # ------------------------------------------------------------------
    # Qdrant memory
    # ------------------------------------------------------------------

    def _get_qdrant(self):
        if self._qdrant is None:
            from qdrant_client import QdrantClient  # noqa: PLC0415
            host, port = self._parse_qdrant_url()
            self._qdrant = QdrantClient(host=host, port=port)
        return self._qdrant

    def _parse_qdrant_url(self) -> tuple[str, int]:
        url = self.qdrant_url.rstrip("/")
        if "://" in url:
            url = url.split("://", 1)[1]
        if ":" in url:
            host, port_str = url.rsplit(":", 1)
            return host, int(port_str)
        return url, 6333

    def _collection_name(self, adapter: dict) -> str:
        return f"pair_forge_{self.project_id}_{adapter.get('name', 'default').replace(' ', '_')}"

    def _ensure_collection(self, adapter: dict) -> None:
        from qdrant_client.models import Distance, VectorParams  # noqa: PLC0415
        client     = self._get_qdrant()
        collection = self._collection_name(adapter)
        if collection not in [c.name for c in client.get_collections().collections]:
            client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=1024, distance=Distance.COSINE),
            )

    def _store_memory(self, adapter: dict, error_type: str, error_desc: str, fix: str) -> None:
        if not error_desc:
            return
        try:
            from qdrant_client.models import PointStruct  # noqa: PLC0415
            self._ensure_collection(adapter)
            vectors = self._embed([error_desc])
            if not vectors:
                return
            client = self._get_qdrant()
            client.upsert(
                collection_name=self._collection_name(adapter),
                points=[PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vectors[0],
                    payload={"error_type": error_type, "error_description": error_desc,
                             "suggested_fix": fix, "seen_count": 1,
                             "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                )],
            )
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"Memory store failed: {exc}")

    def _retrieve_memories(self, adapter: dict, query: str, limit: int = 5) -> list[dict]:
        if not query:
            return []
        try:
            self._ensure_collection(adapter)
            vectors = self._embed([query])
            if not vectors:
                return []
            hits = self._get_qdrant().search(
                collection_name=self._collection_name(adapter),
                query_vector=vectors[0], limit=limit,
            )
            return [h.payload for h in hits if h.score > 0.7]
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"Memory retrieve failed: {exc}")
            return []

    def _format_memories(self, memories: list[dict]) -> str:
        return "\n".join(
            f"- Error ({m.get('error_type','?')}): {m.get('error_description','')} "
            f"-> Fix: {m.get('suggested_fix','')}"
            for m in memories
        )

    def _count_patterns(self, adapter: dict) -> int:
        try:
            return self._get_qdrant().count(collection_name=self._collection_name(adapter)).count
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    # Semantic context — RAG + KG
    # ------------------------------------------------------------------

    def _init_semantic_context(self) -> None:
        host, port = self._parse_qdrant_url()
        try:
            from pipeline.rag_index import IngestionRagIndex  # noqa: PLC0415
            self._rag_ctx = IngestionRagIndex(self.project_id, host=host, port=port)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"IngestionRagIndex unavailable: {exc}")
            self._rag_ctx = None

        neo4j_uri = self.config.get("neo4j_uri", "bolt://localhost:7687")
        neo4j_user = self.config.get("neo4j_user", "neo4j")
        neo4j_password = self.config.get("neo4j_password", "mlops")
        try:
            from pipeline.utils.knowledge_graph import ProjectKG  # noqa: PLC0415
            self._kg = ProjectKG(self.project_id, neo4j_uri, neo4j_user, neo4j_password)
            # Load ontology from CDC if present
            ontology = self.config.get("ontology")
            if ontology:
                self._kg.ingest_ontology(ontology)
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"ProjectKG unavailable: {exc}")
            self._kg = None

    def _search_rag(self, query: str) -> str:
        try:
            return self._rag_ctx.search(query, k=5)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return ""

    def _search_kg(self, text: str) -> str:
        try:
            # Extract simple entity candidates: capitalized words > 3 chars
            import re  # noqa: PLC0415
            entities = list(set(re.findall(r'\b[A-Z][a-zA-Z]{3,}\b', text)))[:10]
            return self._kg.search_context(entities)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return ""

    def _get_kg_constraints(self) -> list[str]:
        try:
            return self._kg.get_constraints()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return []

    def _rag_ctx_count(self) -> int:
        try:
            return self._rag_ctx.count  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return 0

    def _kg_available(self) -> bool:
        return self._kg is not None and getattr(self._kg, "_available", False)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _save_pairs(self, pairs: list[dict], source: str, adapter: dict) -> int:
        if not pairs:
            return 0
        raw_dir = Path(self.config.get("data", {}).get("raw", f"output/{self.project_id}/raw/"))
        raw_dir.mkdir(parents=True, exist_ok=True)
        ts          = time.strftime("%Y%m%d_%H%M%S")
        out_path    = raw_dir / f"pairs_{source}_{ts}.jsonl"
        ingested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with out_path.open("w", encoding="utf-8") as f:
            for pair in pairs:
                f.write(json.dumps({
                    "prompt":     pair.get("input", ""),
                    "completion": pair.get("output", ""),
                    "meta": {"source": source, "adapter": adapter.get("name", "default"),
                             "ingested_at": ingested_at, "origin": "pair-forge"},
                }, ensure_ascii=False) + "\n")
        return len(pairs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_adapter(self, name: str | None) -> dict:
        adapters = self.config.get("adapters", [])
        if name:
            for a in adapters:
                if a.get("name") == name:
                    return a
        return adapters[0] if adapters else {"name": "default", "description": ""}

    def _info(self, msg: str)  -> dict: return {"level": "info",    "msg": msg}
    def _warn(self, msg: str)  -> dict: return {"level": "warning", "msg": msg}
    def _error(self, msg: str) -> dict: return {"level": "error",   "msg": msg}

    @staticmethod
    def _strip_markdown(raw: str) -> str:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        return raw.strip()

    def _parse_json_list(self, raw: str) -> list[dict]:
        try:
            raw   = self._strip_markdown(raw)
            start = raw.find("[")
            end   = raw.rfind("]") + 1
            return json.loads(raw[start:end]) if start != -1 and end > 0 else []
        except Exception:  # noqa: BLE001
            return []

    def _parse_json_object(self, raw: str) -> dict | None:
        try:
            raw   = self._strip_markdown(raw)
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            return json.loads(raw[start:end]) if start != -1 and end > 0 else None
        except Exception:  # noqa: BLE001
            return None
