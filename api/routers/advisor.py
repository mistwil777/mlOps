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

CRITICAL RULE — LLM call points only:
When a technical document describing an existing system is provided, your FIRST task is \
to identify which components make actual LLM/generative AI calls, and which are \
deterministic code. You MUST ONLY propose adapters for the LLM call points.
NEVER propose adapters for: AST parsing, graph algorithms (topological sort, \
betweenness centrality), static analysis tools (Checkstyle, PMD, Ruff), \
rule-based transformations, compilation (javac, ast.parse), file system operations, \
vector store ingestion/chunking, orchestration state machines, or any processing \
that does not require language model generation.
If a component already has a deterministic implementation in the existing system, \
proposing a LoRA adapter for it creates a useless duplicate — do not do it.
The goal is to REPLACE the existing cloud LLM calls (e.g. AWS Bedrock Nova Pro) \
with fine-tuned local adapters, not to reimplementent the whole system.

Other rules:
- Each adapter covers a precise sub-domain (not a broad domain)
- Always include exactly one orchestrator adapter whose role is routing + synthesis
- RAG should only be recommended when the domain requires frequent reference to \
  up-to-date external documents (laws, regulations, technical manuals)
- For embedded/classified deployments: flag rag_recommended=false and suggest \
  adapter fusion at deploy time
- Estimated pairs must be realistic for LoRA fine-tuning (200-2000 per adapter)
- Output ONLY valid JSON, no markdown, no explanation outside the JSON structure
- All text fields (description, pros, cons, team_note, notes, data_types, data_sources) \
  MUST be written in French
"""

_USER_TEMPLATE = """\
Client need: {need}

If a technical document was provided above, before proposing any adapter you MUST \
reason through which components of the described system make actual LLM/generative \
calls vs. which are deterministic. Only the LLM call points become adapters. \
List any existing deterministic implementation that should NOT be duplicated.

For each adapter score it:
- score (0-20): standalone quality
- team_score (0-20): fit within the team — penalise overlaps, reward gap-filling
- pros: 2-3 short strings
- cons: 1-2 short strings
- team_note: one sentence

For each adapter also include "source_configs": a list of typed data sources.
Rules for collector_type:
  - domain=code AND task_type contains "migration" or "translation" → use "rosetta_code" first
  - web documentation, tutorials, articles, legal texts → use "crawl4ai"
  - proprietary or internal data only → use "manual"
For rosetta_code: set value to the Rosetta Code topic keyword (e.g. "sorting", "string manipulation", "file io")
For crawl4ai: set value to the target URL

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
        {{"label": "Rosetta Code — Python/Java", "collector_type": "rosetta_code", "value": "sorting", "estimated_pairs": 200}},
        {{"label": "Documentation officielle Python", "collector_type": "crawl4ai", "value": "https://docs.python.org/3/", "estimated_pairs": 100}}
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
  "notes": "deployment notes"
}}
"""


class AdvisorRequest(BaseModel):
    project: Optional[str] = None
    need: str
    model: str = _DEFAULT_MODEL
    doc_contexts: list[str] = []


def _call_claude(model: str, need: str, doc_contexts: list[str] | None = None) -> str:
    user_content = _USER_TEMPLATE.format(need=need)
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


def _update_project_config(project: str, recommendation: dict[str, Any]) -> None:
    path = Path("config/projects") / f"{project}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")

    cfg = yaml.safe_load(path.read_text())

    adapter_entries = []
    for a in recommendation.get("adapters", []):
        adapter_entries.append({
            "name": a["name"],
            "role": a["role"],
            "status": "pending",
            "rag": a.get("rag_recommended", False),
            "estimated_pairs": a.get("estimated_pairs", 500),
        })

    orch = recommendation.get("orchestrator", {})
    if orch:
        adapter_entries.append({
            "name": orch["name"],
            "role": "orchestrator",
            "status": "pending",
            "rag": False,
            "estimated_pairs": orch.get("estimated_pairs", 200),
        })

    cfg["multi_adapter"] = True
    cfg["adapters"] = adapter_entries

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

Also provide:
- overall: weighted average (coverage*0.4 + cohesion*0.3 + balance*0.15 + orchestrability*0.15)
- gaps: list of sub-domains NOT covered that would be important
- overlaps: list of adapter pairs that risk overlapping
- recommendation: one sentence on what to change to improve the team

Respond with JSON matching exactly:
{{
  "coverage": 14,
  "cohesion": 16,
  "balance": 12,
  "orchestrability": 18,
  "overall": 15.0,
  "gaps": ["gap 1"],
  "overlaps": [["adapter-a", "adapter-b"]],
  "recommendation": "one sentence"
}}
All text fields (gaps, recommendation) MUST be in French.
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

Respond with a single JSON object (no markdown, no prose):
{{
  "name": "kebab-case-name",
  "role": "specialist",
  "description": "one sentence",
  "data_types": ["type1"],
  "data_sources": ["source1"],
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

Respond ONLY with a JSON array (no markdown):
[
  {{
    "type": "clarify" | "merge" | "remove",
    "label": "short action label (French)",
    "explanation": "one sentence in French explaining why this resolves the overlap",
    "adapters": [
      {{
        "name": "kebab-case-name",
        "role": "specialist",
        "description": "updated description",
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
            max_tokens=2500,
            system=[{"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
    except anthropic.APIError as exc:
        raise HTTPException(502, f"Anthropic API error: {exc}")

    try:
        result = _parse_recommendation(raw)
        return result if isinstance(result, list) else []
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
            _update_project_config(body.project, recommendation)  # type: ignore[arg-type]
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
