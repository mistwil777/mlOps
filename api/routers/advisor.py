"""Project advisor — recommends adapter architecture from a free-text client need."""

from __future__ import annotations

import io
import json
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

Rules:
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

For each adapter score it:
- score (0-20): standalone quality
- team_score (0-20): fit within the team — penalise overlaps, reward gap-filling
- pros: 2-3 short strings
- cons: 1-2 short strings
- team_note: one sentence

Return ONLY a raw JSON object (no markdown, no prose, start immediately with {{):
{{
  "adapters": [
    {{
      "name": "kebab-case-name",
      "role": "specialist",
      "description": "one sentence",
      "data_types": ["type1", "type2"],
      "data_sources": ["source1", "source2"],
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
