"""Knowledge Graph per project — Neo4j backend.

One Neo4j database per project (mlops_{project_id}).
Stores: entities (nodes), relations (edges), constraints (properties).

Populated from:
  - CDC ontology section (declared by project owner)
  - Norm documents (entities extracted by LLM during ingestion-context upload)

Used by PairForgeAgent to inject semantic context at pair generation time.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger("mlops.knowledge_graph")

_DEFAULT_URI      = "bolt://localhost:7687"
_DEFAULT_USER     = "neo4j"
_DEFAULT_PASSWORD = "mlops"


class ProjectKG:
    """Knowledge graph scoped to one MLOps project."""

    def __init__(
        self,
        project_id: str,
        uri: str = _DEFAULT_URI,
        user: str = _DEFAULT_USER,
        password: str = _DEFAULT_PASSWORD,
    ) -> None:
        self.project_id = project_id
        self._uri       = uri
        self._user      = user
        self._password  = password
        self._driver    = None
        self._available = False
        self._connect()

    def _connect(self) -> None:
        try:
            from neo4j import GraphDatabase  # noqa: PLC0415

            self._driver    = GraphDatabase.driver(self._uri, auth=(self._user, self._password))
            self._driver.verify_connectivity()
            self._available = True
            self._ensure_constraints()
            logger.info("Neo4j connected — project: %s", self.project_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j unavailable (%s) — KG features disabled", exc)
            self._driver    = None
            self._available = False

    def _session(self):
        if not self._available or self._driver is None:
            raise RuntimeError("Neo4j not available")
        return self._driver.session(database="neo4j")

    def _ensure_constraints(self) -> None:
        """Create uniqueness constraints for Entity nodes scoped to this project."""
        try:
            with self._session() as s:
                s.run(
                    "CREATE CONSTRAINT mlops_entity_unique IF NOT EXISTS "
                    "FOR (e:Entity) REQUIRE (e.project_id, e.name) IS UNIQUE"
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Constraint creation skipped: %s", exc)

    # ── Write ──────────────────────────────────────────────────────────────────

    def upsert_entity(self, label: str, name: str, props: dict[str, Any] | None = None) -> None:
        if not self._available:
            return
        p = props or {}
        try:
            with self._session() as s:
                s.run(
                    "MERGE (e:Entity {project_id: $pid, name: $name}) "
                    "SET e.label = $label, e.description = $desc, e.domain_label = $dl",
                    pid=self.project_id, name=name, label=label,
                    desc=p.get("description", ""), dl=label,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_entity failed: %s", exc)

    def upsert_relation(
        self,
        from_name: str,
        rel_type: str,
        to_name: str,
        props: dict[str, Any] | None = None,
    ) -> None:
        if not self._available:
            return
        safe_rel = re.sub(r"[^A-Z0-9_]", "_", rel_type.upper())
        p = props or {}
        try:
            with self._session() as s:
                s.run(
                    f"MATCH (a:Entity {{project_id: $pid, name: $from_name}}) "
                    f"MATCH (b:Entity {{project_id: $pid, name: $to_name}}) "
                    f"MERGE (a)-[r:{safe_rel}]->(b) "
                    f"SET r.source = $source",
                    pid=self.project_id, from_name=from_name, to_name=to_name,
                    source=p.get("source", "ontology"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_relation failed (%s → %s): %s", from_name, to_name, exc)

    def add_constraint(self, constraint_text: str) -> None:
        if not self._available:
            return
        try:
            with self._session() as s:
                s.run(
                    "MERGE (c:Constraint {project_id: $pid, text: $text})",
                    pid=self.project_id, text=constraint_text,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_constraint failed: %s", exc)

    # ── Ontology ingestion ─────────────────────────────────────────────────────

    def ingest_ontology(self, ontology: dict) -> None:
        """Populate KG from CDC ontology section.

        Expected format:
            entities: [{label, description}]
            relations: [{from, type, to}]
            constraints: [str]
        """
        if not ontology:
            return
        for ent in ontology.get("entities", []):
            self.upsert_entity(ent["label"], ent["label"], {"description": ent.get("description", "")})
        for rel in ontology.get("relations", []):
            self.upsert_relation(rel["from"], rel["type"], rel["to"])
        for c in ontology.get("constraints", []):
            self.add_constraint(c)
        logger.info(
            "Ontology ingested — %d entities, %d relations, %d constraints",
            len(ontology.get("entities", [])),
            len(ontology.get("relations", [])),
            len(ontology.get("constraints", [])),
        )

    def ingest_norm_document(self, text: str, domain: str, llm_fn=None) -> None:
        """Extract entities and relations from a norm document via LLM, store in KG."""
        if not self._available:
            return
        if llm_fn is None:
            logger.debug("No LLM provided for norm extraction — skipping")
            return

        prompt = (
            f"Extract entities and relations from this {domain} domain document.\n"
            "Return JSON with keys:\n"
            '  "entities": [{"label": str, "name": str, "description": str}]\n'
            '  "relations": [{"from": str, "type": str, "to": str}]\n'
            "Only include domain-specific concepts. Be concise.\n\n"
            f"Document (first 4000 chars):\n{text[:4000]}"
        )
        try:
            raw = llm_fn(prompt, {}, 1024)
            if not raw:
                return
            # Extract JSON block
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return
            data = json.loads(match.group())
            for ent in data.get("entities", []):
                self.upsert_entity(ent.get("label", "Entity"), ent.get("name", ""), ent)
            for rel in data.get("relations", []):
                self.upsert_relation(rel.get("from", ""), rel.get("type", "RELATED_TO"), rel.get("to", ""))
            logger.info("Norm document ingested into KG: %d entities, %d relations",
                        len(data.get("entities", [])), len(data.get("relations", [])))
        except Exception as exc:  # noqa: BLE001
            logger.warning("norm_document ingestion failed: %s", exc)

    # ── Query ──────────────────────────────────────────────────────────────────

    def search_context(self, query_entities: list[str], depth: int = 2) -> str:
        """Return a text block of relevant triples for the given entity names.

        Falls back to empty string if Neo4j is unavailable.
        """
        if not self._available or not query_entities:
            return ""
        try:
            with self._session() as s:
                result = s.run(
                    """
                    MATCH (a:Entity {project_id: $pid})
                    WHERE any(q IN $queries WHERE toLower(a.name) CONTAINS toLower(q)
                                             OR toLower(a.label) CONTAINS toLower(q))
                    CALL apoc.path.subgraphAll(a, {maxLevel: $depth, relationshipFilter: '>'})
                    YIELD nodes, relationships
                    RETURN nodes, relationships
                    LIMIT 50
                    """,
                    pid=self.project_id, queries=query_entities, depth=depth,
                )
                lines: list[str] = []
                seen: set[str] = set()
                for record in result:
                    for rel in record["relationships"]:
                        triple = (
                            f"{rel.start_node['name']} [{rel.start_node.get('label','Entity')}]"
                            f" --{rel.type}--> "
                            f"{rel.end_node['name']} [{rel.end_node.get('label','Entity')}]"
                        )
                        if triple not in seen:
                            seen.add(triple)
                            lines.append(triple)
                return "\n".join(lines[:30])  # cap at 30 triples
        except Exception as exc:  # noqa: BLE001
            # APOC not available — fallback to simple 1-hop query
            return self._search_context_simple(query_entities)

    def _search_context_simple(self, query_entities: list[str]) -> str:
        """1-hop fallback query without APOC."""
        try:
            with self._session() as s:
                result = s.run(
                    """
                    MATCH (a:Entity {project_id: $pid})-[r]->(b:Entity {project_id: $pid})
                    WHERE any(q IN $queries WHERE toLower(a.name) CONTAINS toLower(q)
                                             OR toLower(b.name) CONTAINS toLower(q))
                    RETURN a.name AS from_name, a.label AS from_label,
                           type(r) AS rel_type,
                           b.name AS to_name, b.label AS to_label
                    LIMIT 30
                    """,
                    pid=self.project_id, queries=query_entities,
                )
                lines = [
                    f"{row['from_name']} [{row['from_label']}] --{row['rel_type']}--> "
                    f"{row['to_name']} [{row['to_label']}]"
                    for row in result
                ]
                return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            logger.debug("KG search_context failed: %s", exc)
            return ""

    def get_constraints(self) -> list[str]:
        """Return all declared constraints for this project."""
        if not self._available:
            return []
        try:
            with self._session() as s:
                result = s.run(
                    "MATCH (c:Constraint {project_id: $pid}) RETURN c.text AS text",
                    pid=self.project_id,
                )
                return [row["text"] for row in result]
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_constraints failed: %s", exc)
            return []

    def close(self) -> None:
        if self._driver:
            self._driver.close()
