"""Project advisor — recommends adapter architecture from a free-text client need."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic
import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_client = anthropic.Anthropic()
_DEFAULT_MODEL = "claude-sonnet-4-6"

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
"""

_USER_TEMPLATE = """\
Client need: {need}

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
      "estimated_pairs": 800
    }}
  ],
  "orchestrator": {{
    "name": "kebab-case-name",
    "role": "orchestrator",
    "description": "one sentence",
    "data_types": ["routing examples", "question → adapters → synthesized answer"],
    "estimated_pairs": 200
  }},
  "rag_global": false,
  "notes": "any deployment notes"
}}
"""


class AdvisorRequest(BaseModel):
    project: str
    need: str
    model: str = _DEFAULT_MODEL


def _call_claude(model: str, need: str) -> str:
    response = _client.messages.create(
        model=model,
        max_tokens=2048,
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

    _update_project_config(body.project, recommendation)

    n_specialists = len(recommendation.get("adapters", []))
    total_pairs = sum(a.get("estimated_pairs", 0) for a in recommendation.get("adapters", []))
    total_pairs += recommendation.get("orchestrator", {}).get("estimated_pairs", 0)

    return {
        "project": body.project,
        "n_specialists": n_specialists,
        "total_estimated_pairs": total_pairs,
        "recommendation": recommendation,
        "config_updated": f"config/projects/{body.project}.yaml",
    }
