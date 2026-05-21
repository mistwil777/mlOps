"""Project advisor — recommends adapter architecture from a free-text client need."""

from __future__ import annotations

import asyncio
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

import anthropic
import yaml
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_client = anthropic.AnthropicBedrock()
_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"

_SOURCES_DIR   = Path("config/sources")
_LANGUAGES_CFG = Path("config/languages.yaml")


def _build_source_catalogue() -> str:
    """Build a compact source catalogue string from config/sources/*.yaml and config/languages.yaml.

    Injected into the advisor prompt so the LLM picks validated sources
    instead of hallucinating URLs.
    """
    lines: list[str] = []

    # ── domain source registries ──────────────────────────────────────────────
    if _SOURCES_DIR.exists():
        for src_file in sorted(_SOURCES_DIR.glob("*.yaml")):
            domain = src_file.stem
            try:
                data = yaml.safe_load(src_file.read_text()) or {}
            except Exception:
                continue
            topics = data.get("topics", {})
            if not topics:
                continue
            lines.append(f"=== Domain: {domain} ===")
            for topic, buckets in topics.items():
                lines.append(f"  Topic: {topic}")
                for priority in ("official_docs", "official_repo", "quality_repos", "github_search"):
                    urls = buckets.get(priority, [])
                    if urls:
                        tag = {"official_docs": "doc_crawl", "official_repo": "web_crawl",
                               "quality_repos": "web_crawl", "github_search": "web_crawl"}[priority]
                        for u in urls[:3]:  # cap to keep prompt size reasonable
                            lines.append(f"    [{tag}] [{priority}] {u}")

    # ── fusible language pairs ────────────────────────────────────────────────
    if _LANGUAGES_CFG.exists():
        try:
            lang_data = yaml.safe_load(_LANGUAGES_CFG.read_text()) or {}
        except Exception:
            lang_data = {}
        pairs = lang_data.get("pairs", {})
        if pairs:
            lines.append("=== Fusible language pairs (doc_crawl + pair_fuser) ===")
            for key, cfg in pairs.items():
                src = cfg.get("source_lang", "")
                tgt = cfg.get("target_lang", "")
                desc = cfg.get("description", "")
                lines.append(f"  Pair key: {key} ({src} → {tgt}) — {desc}")
                lines.append(f"    fusible=true  lang_pair_key={key}")
                for lang, sources in cfg.get("doc_sources", {}).items():
                    for s in sources:
                        lines.append(
                            f"    [doc_crawl] lang_hint={lang}  url={s['url']}"
                        )

    return "\n".join(lines) if lines else "(no source catalogue available)"
_HAIKU_MODEL   = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_MAX_DOC_CHARS = 40_000   # au-delà on résume via Haiku avant envoi


# ── Extraction de texte depuis documents ──────────────────────────────────────

def _extract_text_pdf(data: bytes) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(
                page.extract_text() or "" for page in pdf.pages
            ).strip()
    except ImportError:
        raise HTTPException(422, "pdfplumber non installé — pip install pdfplumber")


def _extract_text_docx(data: bytes) -> str:
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except ImportError:
        raise HTTPException(422, "python-docx non installé — pip install python-docx")


def _extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return _extract_text_pdf(data)
    if ext in (".docx", ".doc"):
        return _extract_text_docx(data)
    if ext in (".txt", ".md", ".rst", ".csv"):
        return data.decode("utf-8", errors="replace").strip()
    raise HTTPException(415, f"Format non supporté : {ext}. Acceptés : PDF, DOCX, TXT, MD")


def _maybe_summarize(text: str, filename: str) -> str:
    """Si le texte dépasse _MAX_DOC_CHARS, le résume via Haiku (peu cher)."""
    if len(text) <= _MAX_DOC_CHARS:
        return text
    safe_name = Path(filename).name[:80]  # basename seulement, tronqué
    prompt = (
        f"Summarize the following technical document '{safe_name}' in French, preserving "
        "all functional requirements, integration points, technical constraints, existing "
        "modules, data models, and API contracts. Be exhaustive on technical details. "
        "Output plain text, no markdown headers.\n\n"
        + text[:120_000]  # limite de sécurité
    )
    response = _client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return f"[RÉSUMÉ AUTOMATIQUE — {filename}]\n{response.content[0].text}"


@router.post("/extract-doc")
async def extract_doc(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:  # 20 MB max
        raise HTTPException(413, "Fichier trop volumineux (max 20 MB)")
    text = _extract_text(file.filename or "document", data)
    if not text:
        raise HTTPException(422, "Impossible d'extraire du texte de ce fichier")
    was_summarized = len(text) > _MAX_DOC_CHARS
    summary = _maybe_summarize(text, file.filename or "document")
    return {
        "filename": file.filename or "document",
        "char_count": len(text),
        "was_summarized": was_summarized,
        "text": summary,
    }

_SYSTEM_PROMPT = """\
You are an expert MLOps architect specialising in fine-tuned LLM systems for regulated \
industries (Defense, Legal, HR). Given a client need, you design a multi-adapter \
architecture using small, specialized LoRA adapters on top of a shared base model.

CORE PRINCIPLE — AI only where it adds real value (Green IT):
Never propose a LoRA adapter for a task that can be done reliably with deterministic \
code: AST parsing, graph algorithms, sorting, rule-based transformations, static \
analysis, compilation checks, file system operations, state machine routing. \
These must be implemented as software, not as AI models. \
AI adds value for: natural language understanding, generative code transformation \
between paradigms, semantic retrieval, ambiguous classification, context-dependent \
text generation.

TWO MODES — apply the correct one:

MODE A — Existing system (a technical document describing a running system is provided):
Your task is to identify which components make actual LLM/generative calls and replace \
ONLY those with fine-tuned local adapters. Do NOT propose adapters for components that \
already have a deterministic implementation — that would be a useless duplicate. \
The goal is on-premise sovereignty: replace cloud LLM calls (e.g. AWS Bedrock, OpenAI) \
with local fine-tuned models, nothing else.

MODE B — Greenfield (no existing system, only a business need):
Reason through the full solution: which sub-tasks require LLM generation → become \
adapters; which sub-tasks are better served by deterministic software → list them in \
the "software_components" field of your JSON output so the engineering team builds \
them as code. Be explicit about the boundary.

Other rules:
- Each adapter covers a precise sub-domain (not a broad domain)
- Always include exactly one orchestrator adapter whose role is routing + synthesis
- RAG should only be recommended when the domain requires frequent reference to \
  up-to-date external documents (laws, regulations, technical manuals)
- For embedded/classified deployments: flag rag_recommended=false and suggest \
  adapter fusion at deploy time
- Estimated pairs must be realistic for LoRA fine-tuning (200-2000 per adapter)
- Output ONLY valid JSON, no markdown, no explanation outside the JSON structure
- All text fields (description, pros, cons, team_note, notes, data_types, \
  data_sources, software_components) MUST be written in French
"""

_USER_TEMPLATE = """\
Client need: {need}

Step 1 — Determine the mode:
- If a technical document describing an EXISTING system was provided: apply MODE A. \
  Identify only the components that make LLM/generative calls. Each becomes one adapter. \
  Deterministic components already implemented must NOT become adapters.
- If no existing system is described (greenfield): apply MODE B. \
  Reason through the full solution. For each sub-task, decide: LLM needed → adapter; \
  deterministic code sufficient → add to "software_components" list.

Step 2 — For each adapter score it:
- score (0-20): standalone quality
- team_score (0-20): fit within the team — penalise overlaps, reward gap-filling
- pros: 2-3 short strings
- cons: 1-2 short strings
- team_note: one sentence

For each adapter also include "source_configs": a list of typed data sources.

MANDATORY: use the Source Catalogue below — do NOT invent URLs.
Rules for collector_type:
  - domain=code AND task_type contains "migration" or "translation" → "multilang_wiki" first, then "doc_crawl" for official language docs
  - official documentation site listed as [doc_crawl] in the catalogue → use "doc_crawl" (NOT "crawl4ai"), add "lang_hint" field matching the language
  - other crawlable web pages (repos, articles) → "web_crawl"
  - proprietary or internal data only → "manual"

For multilang_wiki: set value to the topic keyword used by the wiki source (e.g. "sort", "string", "fibonacci" for Rosetta Code)
For doc_crawl: set value to the exact URL from the catalogue, add "lang_hint": "<language>" (e.g. "python", "java", "typescript")
For web_crawl: set value to the target URL

FUSION: if two adapters use doc_crawl for a language pair that appears in the Fusible pairs section,
add "fusible": true and "lang_pair_key": "<key>" (e.g. "python-java") to EACH of those source_configs entries.
This signals that the pair_fuser can generate aligned migration pairs from those two doc sets.

{source_catalogue}

Return ONLY a raw JSON object (no markdown, no prose, start immediately with {{):
{{
  "adapters": [
    {{
      "name": "kebab-case-name",
      "role": "specialist",
      "description": "one sentence",
      "data_types": ["type1", "type2"],
      "data_sources": ["source1", "source2"],
      "source_configs": [
        {{"label": "Rosetta Code — tri", "collector_type": "multilang_wiki", "value": "sort", "estimated_pairs": 80}},
        {{"label": "Docs Python — tutorial", "collector_type": "doc_crawl", "value": "https://docs.python.org/fr/3/tutorial/", "lang_hint": "python", "estimated_pairs": 80, "fusible": true, "lang_pair_key": "python-java"}},
        {{"label": "Docs Java — language basics", "collector_type": "doc_crawl", "value": "https://dev.java/learn/language-basics/", "lang_hint": "java", "estimated_pairs": 60, "fusible": true, "lang_pair_key": "python-java"}}
      ],
      "rag_recommended": true,
      "estimated_pairs": 800,
      "score": 16,
      "team_score": 14,
      "pros": ["avantage 1", "avantage 2"],
      "cons": ["inconvénient 1"],
      "team_note": "une phrase"
    }}
  ],
  "orchestrator": {{
    "name": "kebab-case-name",
    "role": "orchestrator",
    "description": "one sentence",
    "data_types": ["routing examples"],
    "source_configs": [],
    "estimated_pairs": 200,
    "score": 15,
    "team_score": 18,
    "pros": ["avantage 1"],
    "cons": ["inconvénient 1"],
    "team_note": "une phrase"
  }},
  "rag_global": false,
  "notes": "deployment notes",
  "software_components": [
    "composant déterministe 1 — description courte",
    "composant déterministe 2 — description courte"
  ]
}}
"""


class AdvisorRequest(BaseModel):
    project: Optional[str] = None
    need: str
    model: str = _DEFAULT_MODEL
    doc_contexts: list[str] = []


def _call_claude(model: str, need: str, doc_contexts: list[str] | None = None) -> str:
    catalogue = _build_source_catalogue()
    user_content = _USER_TEMPLATE.format(need=need, source_catalogue=catalogue)
    if doc_contexts:
        ctx_block = "\n\n".join(
            f"[CONTEXTE TECHNIQUE — Document {i+1}]\n{ctx}"
            for i, ctx in enumerate(doc_contexts)
            if ctx.strip()
        )
        if ctx_block:
            user_content = (
                f"[CONTEXTE TECHNIQUE — Documents fournis par le client]\n{ctx_block}"
                f"\n\n[FIN DU CONTEXTE]\n\n{user_content}"
            )
    response = _client.messages.create(
        model=model,
        max_tokens=8192,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": user_content},
        ],
    )
    return response.content[0].text


def _parse_recommendation(raw: str) -> dict[str, Any]:
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        clean = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(clean)


def _update_project_config(project: str, recommendation: dict[str, Any], need: str = "") -> None:
    path = Path("config/projects") / f"{project}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")

    cfg = yaml.safe_load(path.read_text())

    existing_by_name: dict[str, dict] = {
        a["name"]: a for a in cfg.get("adapters", []) if isinstance(a, dict)
    }

    adapter_entries = []
    all_adapters = list(recommendation.get("adapters", []))
    orch = recommendation.get("orchestrator", {})
    if orch:
        all_adapters.append(orch)

    for a in all_adapters:
        existing = existing_by_name.get(a["name"], {})
        entry: dict[str, Any] = {
            "name":            a["name"],
            "role":            a["role"],
            "description":     a.get("description", existing.get("description", "")),
            "estimated_pairs": a.get("estimated_pairs", existing.get("estimated_pairs", 200)),
            "rag_recommended": a.get("rag_recommended", existing.get("rag_recommended", False)),
            "data_sources":    a.get("data_sources", existing.get("data_sources", [])),
        }
        # Persist LLM-generated evaluation fields so they survive page reload
        for field in ("data_types", "score", "team_score", "pros", "cons", "team_note"):
            val = a.get(field) if a.get(field) is not None else existing.get(field)
            if val is not None:
                entry[field] = val

        src_cfgs = a.get("source_configs") or existing.get("source_configs") or []
        if src_cfgs:
            entry["source_configs"] = src_cfgs
        adapter_entries.append(entry)

    cfg["multi_adapter"] = True
    cfg["adapters"] = adapter_entries

    # Persist top-level recommendation context
    if recommendation.get("notes"):
        cfg["advisor_notes"] = recommendation["notes"]
    if recommendation.get("software_components"):
        cfg["software_components"] = recommendation["software_components"]
    if recommendation.get("rag_global") is not None:
        cfg["rag_global"] = recommendation["rag_global"]
    if need:
        cfg["advisor_need"] = need

    if cfg.get("classification") in ("confidentiel", "secret"):
        cfg["isolation_level"] = "sovereign"

    path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))


_ALT_TEMPLATE = """\
The user is building a multi-adapter MLOps system.
Original client need: {need}

Current selection of adapters already chosen:
{current_selection}

The following adapter was proposed but the user wants alternatives:
- Name: {adapter_name}
- Description: {adapter_desc}

Propose exactly 3 alternative adapters that could replace this one.
Each alternative should cover a different sub-angle of the same problem space.

For each alternative, also evaluate:
1. Its individual quality as a standalone specialist (score 0-20)
2. Its fit within the CURRENT selection (team_score 0-20): does it complement the other adapters
   without overlapping them? Does it fill a real gap? A high individual score but overlap with
   existing adapters should lower team_score.

Respond with a JSON array matching exactly this schema:
[
  {{
    "name": "kebab-case-name",
    "role": "specialist",
    "description": "one sentence",
    "data_types": ["list of data types"],
    "data_sources": ["list of sources"],
    "rag_recommended": true,
    "estimated_pairs": 600,
    "score": 15,
    "team_score": 12,
    "pros": ["advantage 1", "advantage 2"],
    "cons": ["drawback 1", "drawback 2"],
    "team_note": "one sentence explaining the team fit score"
  }}
]
All text fields MUST be in French.
Output ONLY valid JSON, no markdown, no explanation.
"""

_EVAL_TEMPLATE = """\
The user is building a multi-adapter MLOps system for the following client need:
{need}

Evaluate the COHERENCE and COMPLETENESS of this adapter selection as a team:
{selection}

Score the selection on these axes (0-20 each):
- coverage: does it cover the full scope of the client need?
- cohesion: do the adapters form a coherent team without major overlaps?
- balance: is the workload distribution balanced (estimated_pairs)?
- orchestrability: can a single orchestrator route between them effectively?

STRICT RULES for gaps and overlaps — read carefully:

gaps: List ONLY sub-domains that are completely absent AND critical for the client need.
Do NOT report gaps for: edge cases, rare scenarios, future extensions, or anything the \
orchestrator can handle. Maximum 3 gaps. If the selection is reasonably complete, return [].

overlaps: Report ONLY pairs where an orchestrator CANNOT distinguish which adapter to call \
because their input/output contracts are IDENTICAL in practice — not just semantically related.
Two adapters that share a domain but have clearly different scopes (e.g. one handles source \
files, the other handles test files) are NOT overlapping. Maximum 2 overlaps. \
If routing is unambiguous, return [].
Each overlap MUST be a two-element array of adapter NAMES (exact strings from the selection), \
e.g. ["syntax_translator", "type_system_adapter"]. NOT a description string.

Also provide:
- overall: weighted average (coverage*0.4 + cohesion*0.3 + balance*0.15 + orchestrability*0.15)
- recommendation: one sentence on what to change to improve the team

Respond with JSON matching exactly:
{{
  "coverage": 14,
  "cohesion": 16,
  "balance": 12,
  "orchestrability": 18,
  "overall": 15.0,
  "gaps": ["lacune 1 en français", "lacune 2 en français"],
  "overlaps": [["adapter_name_a", "adapter_name_b"]],
  "recommendation": "one sentence in French"
}}
gaps: strings in French describing the missing sub-domain.
overlaps: arrays of exactly TWO adapter names (exact strings from the selection above).
recommendation: one sentence in French.
Output ONLY valid JSON, no markdown, no explanation.
"""


class AlternativeRequest(BaseModel):
    need: str
    adapter_name: str
    adapter_desc: str
    current_selection: list[dict[str, Any]] = []
    model: str = _DEFAULT_MODEL


class EvaluateSelectionRequest(BaseModel):
    need: str
    adapters: list[dict[str, Any]]
    model: str = _DEFAULT_MODEL


@router.post("/alternatives")
def alternatives(body: AlternativeRequest) -> list[dict[str, Any]]:
    selection_str = "\n".join(
        f"- {a['name']}: {a.get('description', '')}" for a in body.current_selection
        if a["name"] != body.adapter_name
    ) or "none yet"

    prompt = _ALT_TEMPLATE.format(
        need=body.need,
        adapter_name=body.adapter_name,
        adapter_desc=body.adapter_desc,
        current_selection=selection_str,
    )
    try:
        response = _client.messages.create(
            model=body.model,
            max_tokens=3000,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    try:
        result = _parse_recommendation(raw)
        return result if isinstance(result, list) else result.get("adapters", [])
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(422, f"Model returned invalid JSON: {exc}\nRaw: {raw[:500]}")


_FILL_GAP_TEMPLATE = """\
The user is building a multi-adapter MLOps system for the following client need:
{need}

Current adapter selection:
{selection}

The evaluation identified this uncovered gap:
"{gap}"

Design exactly ONE new specialist adapter that fills this gap without overlapping the existing ones.
The adapter must:
- Cover ONLY the identified gap, nothing else
- Have a clearly scoped role that does not duplicate any existing adapter
- Be realistic in terms of LoRA fine-tuning (200-2000 pairs)

Also evaluate:
- score (0-20): standalone quality as a specialist
- team_score (0-20): fit within the CURRENT selection — does it fill the gap without overlap?

Rules for collector_type in source_configs:
- multi-language code wiki (e.g. Rosetta Code) → "multilang_wiki"
- official documentation sites → "doc_crawl" (add "lang_hint")
- other crawlable web pages, repos, articles → "web_crawl" (NOT "crawl4ai")
- proprietary or internal data → "manual"

Respond with a single JSON object (no markdown, no prose):
{{
  "name": "kebab-case-name",
  "role": "specialist",
  "description": "one sentence",
  "data_types": ["type1"],
  "data_sources": ["source1"],
  "source_configs": [
    {{"label": "Rosetta Code — topic", "collector_type": "multilang_wiki", "value": "topic", "estimated_pairs": 200}},
    {{"label": "Docs officielle", "collector_type": "doc_crawl", "value": "https://...", "lang_hint": "python", "estimated_pairs": 100}}
  ],
  "rag_recommended": false,
  "estimated_pairs": 600,
  "score": 16,
  "team_score": 18,
  "pros": ["avantage 1", "avantage 2"],
  "cons": ["inconvénient 1"],
  "team_note": "une phrase"
}}
All text fields MUST be in French. Output ONLY valid JSON.
"""

_RESOLVE_OVERLAP_TEMPLATE = """\
The user is building a multi-adapter MLOps system for the following client need:
{need}

Current adapter selection:
{selection}

The evaluation detected a risk of overlap between these two adapters:
- Adapter A — {adapter_a_name}: {adapter_a_desc}
- Adapter B — {adapter_b_name}: {adapter_b_desc}

Propose exactly 3 concrete resolutions. Each resolution is one of:
- "clarify": rewrite the description/boundaries of BOTH adapters to eliminate overlap
- "merge": merge them into a single more powerful adapter
- "remove": remove the less useful one (specify which) and explain why

For each resolution, provide updated adapter definitions (for "remove", only one).

Rules for the "adapters" field:
- "clarify": provide BOTH updated adapters with clarified boundaries (2 items)
- "merge": provide exactly ONE new merged adapter (1 item)
- "remove": provide exactly ONE surviving adapter (1 item), use the name of the one kept

Respond ONLY with a JSON array of exactly 3 objects (no markdown, start with [):
[
  {{
    "type": "clarify",
    "label": "libellé court en français",
    "explanation": "une phrase en français",
    "adapters": [
      {{
        "name": "kebab-case-name",
        "role": "specialist",
        "description": "description clarifiée",
        "data_types": ["type1"],
        "data_sources": ["source1"],
        "rag_recommended": false,
        "estimated_pairs": 600,
        "score": 15,
        "team_score": 17,
        "pros": ["avantage 1"],
        "cons": ["inconvénient 1"],
        "team_note": "une phrase"
      }}
    ]
  }},
  {{
    "type": "merge",
    "label": "libellé court en français",
    "explanation": "une phrase en français",
    "adapters": [{{ ... }}]
  }},
  {{
    "type": "remove",
    "label": "libellé court en français",
    "explanation": "une phrase en français",
    "adapters": [{{ ... }}]
  }}
]
All text fields MUST be in French. Output ONLY valid JSON.
"""


class FillGapRequest(BaseModel):
    need: str
    gap: str
    current_selection: list[dict[str, Any]] = []
    model: str = _DEFAULT_MODEL


class ResolveOverlapRequest(BaseModel):
    need: str
    adapter_a: dict[str, Any]
    adapter_b: dict[str, Any]
    current_selection: list[dict[str, Any]] = []
    model: str = _DEFAULT_MODEL


@router.post("/fill-gap")
def fill_gap(body: FillGapRequest) -> dict[str, Any]:
    selection_str = "\n".join(
        f"- {a['name']} ({a.get('role','specialist')}): {a.get('description','')}"
        for a in body.current_selection
    ) or "none yet"

    prompt = _FILL_GAP_TEMPLATE.format(
        need=body.need,
        gap=body.gap,
        selection=selection_str,
    )
    try:
        response = _client.messages.create(
            model=body.model,
            max_tokens=1200,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    try:
        return _parse_recommendation(raw)
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(422, f"Model returned invalid JSON: {exc}\nRaw: {raw[:500]}")


@router.post("/resolve-overlap")
def resolve_overlap(body: ResolveOverlapRequest) -> list[dict[str, Any]]:
    selection_str = "\n".join(
        f"- {a['name']} ({a.get('role','specialist')}): {a.get('description','')}"
        for a in body.current_selection
        if a["name"] not in (body.adapter_a.get("name"), body.adapter_b.get("name"))
    ) or "none"

    prompt = _RESOLVE_OVERLAP_TEMPLATE.format(
        need=body.need,
        adapter_a_name=body.adapter_a.get("name", ""),
        adapter_a_desc=body.adapter_a.get("description", ""),
        adapter_b_name=body.adapter_b.get("name", ""),
        adapter_b_desc=body.adapter_b.get("description", ""),
        selection=selection_str,
    )
    try:
        response = _client.messages.create(
            model=body.model,
            max_tokens=4096,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    try:
        result = _parse_recommendation(raw)
        if isinstance(result, list):
            return result
        # model sometimes wraps in {"resolutions": [...]} or {"data": [...]}
        for key in ("resolutions", "data", "items", "result"):
            if key in result and isinstance(result[key], list):
                return result[key]
        return []
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(422, f"Model returned invalid JSON: {exc}\nRaw: {raw[:500]}")


@router.post("/evaluate-selection")
def evaluate_selection(body: EvaluateSelectionRequest) -> dict[str, Any]:
    selection_str = "\n".join(
        f"- {a['name']} ({a.get('role','specialist')}): {a.get('description','')} — ~{a.get('estimated_pairs',0)} pairs"
        for a in body.adapters
    )
    prompt = _EVAL_TEMPLATE.format(need=body.need, selection=selection_str)
    try:
        response = _client.messages.create(
            model=body.model,
            max_tokens=800,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    try:
        return _parse_recommendation(raw)
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(422, f"Model returned invalid JSON: {exc}\nRaw: {raw[:500]}")


_PAIR_QUALITY_TEMPLATE = """\
You are a training data quality expert for LLM fine-tuning.
Analyze this training pair for the adapter "{adapter_name}" ({adapter_desc}).

PROMPT:
{prompt}

COMPLETION:
{completion}

Rate it on a scale 0-10 and reply with valid JSON only:
{{"score": <int 0-10>, "issues": ["issue1"], "verdict": "keep"|"fix"|"discard"}}
"""

_PAIR_GENERATE_TEMPLATE = """\
You are a training data generator for a LoRA adapter named "{adapter_name}".
Adapter purpose: {adapter_desc}
Domain need: {need}

Generate exactly 1 high-quality training pair for this adapter.
The pair must be realistic, specific, and ready for fine-tuning.

Reply with valid JSON only (no markdown):
{{"prompt": "the input prompt", "completion": "the expected output"}}
"""

_MANUAL_BRIEF_TEMPLATE = """\
You are an MLOps data architect.
An adapter named "{adapter_name}" ({adapter_desc}) needs training data.
Domain: {need}

Based on the scraped pairs quality analysis below:
{quality_summary}

Write a precise data brief for a human data annotator who will create the remaining pairs manually.
Include: recommended nature, format, topics to cover, number of pairs needed (target: {target_pairs}), priority order.
Write in French. Be actionable and specific.

Reply with plain text (no markdown headers), 150-250 words.
"""


class AnalyzePairsRequest(BaseModel):
    adapter_name: str
    adapter_desc: str
    need: str
    pairs_sample: list[dict[str, Any]]   # subset of raw pairs to analyze
    n_generate: int = 10                  # how many synthetic pairs to generate
    target_pairs: int = 500              # total pairs target for this adapter


async def _score_pair(pair: dict[str, Any], adapter_name: str, adapter_desc: str) -> dict[str, Any]:
    prompt = _PAIR_QUALITY_TEMPLATE.format(
        adapter_name=adapter_name,
        adapter_desc=adapter_desc,
        prompt=pair.get("prompt", "")[:600],
        completion=pair.get("completion", "")[:600],
    )
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            ),
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1] if raw.endswith("```") else raw.splitlines()[1:])
        return {**pair, **json.loads(raw)}
    except Exception:
        return {**pair, "score": 5, "issues": [], "verdict": "keep"}


async def _generate_one_pair(adapter_name: str, adapter_desc: str, need: str) -> dict[str, Any] | None:
    prompt = _PAIR_GENERATE_TEMPLATE.format(
        adapter_name=adapter_name,
        adapter_desc=adapter_desc,
        need=need[:400],
    )
    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: _client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ),
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1] if raw.endswith("```") else raw.splitlines()[1:])
        pair = json.loads(raw)
        if "prompt" in pair and "completion" in pair:
            return pair
        return None
    except Exception:
        return None


@router.post("/analyze-pairs")
async def analyze_pairs(body: AnalyzePairsRequest) -> dict[str, Any]:
    sample = body.pairs_sample[:30]  # cap at 30 pairs to control cost

    # 1. Score all pairs in parallel
    scored = await asyncio.gather(*[
        _score_pair(p, body.adapter_name, body.adapter_desc)
        for p in sample
    ])
    scored_list = list(scored)

    # Quality summary for brief
    n_keep    = sum(1 for p in scored_list if p.get("verdict") == "keep")
    n_fix     = sum(1 for p in scored_list if p.get("verdict") == "fix")
    n_discard = sum(1 for p in scored_list if p.get("verdict") == "discard")
    avg_score = sum(p.get("score", 5) for p in scored_list) / max(len(scored_list), 1)
    all_issues = [i for p in scored_list for i in p.get("issues", [])]

    quality_summary = (
        f"Analyzed {len(scored_list)} pairs: {n_keep} keep, {n_fix} fix, {n_discard} discard. "
        f"Average score: {avg_score:.1f}/10. "
        f"Common issues: {', '.join(set(all_issues[:10])) or 'none'}."
    )

    # 2. Generate n_generate pairs in parallel (batched to avoid rate limits)
    n_gen = min(body.n_generate, 50)
    generated_raw = await asyncio.gather(*[
        _generate_one_pair(body.adapter_name, body.adapter_desc, body.need)
        for _ in range(n_gen)
    ])
    generated = [p for p in generated_raw if p is not None]

    # 3. Manual brief via Haiku
    brief_prompt = _MANUAL_BRIEF_TEMPLATE.format(
        adapter_name=body.adapter_name,
        adapter_desc=body.adapter_desc,
        need=body.need[:400],
        quality_summary=quality_summary,
        target_pairs=body.target_pairs,
    )
    try:
        loop = asyncio.get_event_loop()
        brief_resp = await loop.run_in_executor(
            None,
            lambda: _client.messages.create(
                model=_HAIKU_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": brief_prompt}],
            ),
        )
        manual_brief = brief_resp.content[0].text.strip()
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    quality_report = {
        "total_analyzed": len(scored_list),
        "n_keep": n_keep,
        "n_fix": n_fix,
        "n_discard": n_discard,
        "avg_score": round(avg_score, 2),
        "common_issues": list(set(all_issues[:10])),
        "scored_pairs": scored_list,
    }

    return {
        "adapter_name": body.adapter_name,
        "quality_report": quality_report,
        "generated_pairs": generated,
        "manual_brief": manual_brief,
    }


@router.get("/cross-project-matches")
async def cross_project_matches(adapter_desc: str, k: int = 10) -> dict[str, Any]:
    """
    Semantic match of an adapter description against all indexed datalake pairs.
    Returns aggregate counts by project + freshness info for the wizard.
    """
    from pipeline.datalake_index import DatalakeIndex  # noqa: PLC0415
    loop = asyncio.get_event_loop()
    try:
        idx = await loop.run_in_executor(None, DatalakeIndex)
        results = await loop.run_in_executor(
            None, lambda: idx.match_adapter(adapter_desc, k=k)
        )
    except Exception as exc:
        raise HTTPException(503, f"Datalake index unavailable: {exc}")

    by_project = dict(Counter(r.get("project_id", "unknown") for r in results))
    total = sum(by_project.values())

    # Oldest collected_at for freshness warning
    dates = [r["collected_at"] for r in results if r.get("collected_at")]
    oldest = min(dates) if dates else None

    return {
        "total_matching": total,
        "by_project": by_project,
        "samples": results[:3],
        "oldest_collected_at": oldest,
    }


class PatchAdapterSourcesRequest(BaseModel):
    adapter_name: str
    source_configs: list[dict[str, Any]]


@router.patch("/adapter-sources/{project_id}")
def patch_adapter_sources(project_id: str, body: PatchAdapterSourcesRequest) -> dict[str, Any]:
    """Replace the source_configs of one adapter in the project YAML."""
    path = Path("config/projects") / f"{project_id}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Project '{project_id}' not found")

    cfg = yaml.safe_load(path.read_text()) or {}
    adapters: list[dict] = cfg.get("adapters", [])
    found = False
    for a in adapters:
        if isinstance(a, dict) and a.get("name") == body.adapter_name:
            a["source_configs"] = body.source_configs
            # keep data_sources in sync with label list
            a["data_sources"] = [s.get("label", s.get("value", "")) for s in body.source_configs]
            found = True
            break
    if not found:
        raise HTTPException(404, f"Adapter '{body.adapter_name}' not found in project '{project_id}'")

    cfg["adapters"] = adapters
    path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    return {"ok": True, "adapter_name": body.adapter_name, "n_sources": len(body.source_configs)}


@router.get("/recommendation/{project_id}")
def get_recommendation(project_id: str) -> dict[str, Any]:
    """Return the saved adapter recommendation for an existing project."""
    path = Path("config/projects") / f"{project_id}.yaml"
    if not path.exists():
        raise HTTPException(404, f"Project '{project_id}' not found")
    cfg = yaml.safe_load(path.read_text()) or {}

    all_adapters: list[dict[str, Any]] = cfg.get("adapters", [])
    specialists   = [a for a in all_adapters if a.get("role") != "orchestrator"]
    orchestrators = [a for a in all_adapters if a.get("role") == "orchestrator"]
    orchestrator  = orchestrators[0] if orchestrators else {}

    total_pairs = sum(a.get("estimated_pairs", 0) for a in all_adapters)
    return {
        "project":               project_id,
        "n_specialists":         len(specialists),
        "total_estimated_pairs": total_pairs,
        "config_updated":        None,
        "advisor_need":          cfg.get("advisor_need", ""),
        "recommendation": {
            "adapters":            specialists,
            "orchestrator":        orchestrator,
            "rag_global":          cfg.get("rag_global", False),
            "notes":               cfg.get("advisor_notes") or cfg.get("description", ""),
            "software_components": cfg.get("software_components", []),
        },
    }


@router.post("/recommend")
def recommend(body: AdvisorRequest) -> dict[str, Any]:
    try:
        raw = _call_claude(body.model, body.need, body.doc_contexts or [])
    except anthropic.AuthenticationError:
        raise HTTPException(401, "Invalid Anthropic API key — set ANTHROPIC_API_KEY")
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    try:
        recommendation = _parse_recommendation(raw)
    except (json.JSONDecodeError, KeyError) as exc:
        raise HTTPException(422, f"Model returned invalid JSON: {exc}\nRaw: {raw[:500]}")

    config_updated = None
    if body.project:
        try:
            _update_project_config(body.project, recommendation, need=body.need)  # type: ignore[arg-type]
            config_updated = f"config/projects/{body.project}.yaml"
        except FileNotFoundError:
            pass

    n_specialists = len(recommendation.get("adapters", []))
    total_pairs = sum(a.get("estimated_pairs", 0) for a in recommendation.get("adapters", []))
    total_pairs += recommendation.get("orchestrator", {}).get("estimated_pairs", 0)

    return {
        "project": body.project,
        "n_specialists": n_specialists,
        "total_estimated_pairs": total_pairs,
        "recommendation": recommendation,
        "config_updated": config_updated,
    }
