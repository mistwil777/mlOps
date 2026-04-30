"""Project advisor — recommends adapter architecture from a free-text client need."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import anthropic
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_client = anthropic.AnthropicBedrock()
_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"

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

For each adapter, also score it:
- score (0-20): intrinsic quality as a standalone specialist for this need
- team_score (0-20): fit within the full proposed team — penalise overlaps, reward gap-filling
- pros: 2-3 advantages as a list of short strings
- cons: 1-2 drawbacks as a list of short strings
- team_note: one sentence explaining the team_score

Respond with a JSON object matching exactly this schema:
{{
  "adapters": [
    {{
      "name": "kebab-case-name",
      "role": "specialist",
      "description": "one sentence",
      "data_types": ["list of data types"],
      "data_sources": ["list of sources"],
      "rag_recommended": true,
      "estimated_pairs": 800,
      "score": 16,
      "team_score": 14,
      "pros": ["advantage 1", "advantage 2"],
      "cons": ["drawback 1"],
      "team_note": "one sentence"
    }}
  ],
  "orchestrator": {{
    "name": "kebab-case-name",
    "role": "orchestrator",
    "description": "one sentence",
    "data_types": ["routing examples", "question → adapters → synthesized answer"],
    "estimated_pairs": 200,
    "score": 15,
    "team_score": 18,
    "pros": ["advantage 1"],
    "cons": ["drawback 1"],
    "team_note": "one sentence"
  }},
  "rag_global": false,
  "notes": "any deployment notes"
}}
"""


class AdvisorRequest(BaseModel):
    project: Optional[str] = None
    need: str
    model: str = _DEFAULT_MODEL


def _call_claude(model: str, need: str) -> str:
    response = _client.messages.create(
        model=model,
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": _USER_TEMPLATE.format(need=need)},
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
            max_tokens=1500,
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
        raw = _call_claude(body.model, body.need)
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
