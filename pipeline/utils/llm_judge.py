"""LLM-as-judge pipeline — Sonnet + Haiku consensus for correction validation.

A correction is auto-validated if both models agree it is correct.
If they disagree, it goes to the human validation queue (stays in staging).

Confidence scoring:
  - Both agree VALID   → confidence 0.85, source "llm_consensus", auto-validate
  - Both agree INVALID → confidence 0.85, auto-reject
  - Disagreement       → confidence 0.5, stays staging, human queue
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

_SONNET = "us.anthropic.claude-sonnet-4-6"
_HAIKU  = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_JUDGE_SYSTEM = """Tu es un expert MLOps chargé de valider des corrections de mémoire.
Une correction décrit une incohérence ou erreur observée dans les réponses d'un système advisor.
Tu dois évaluer si la correction est :
- VALIDE : l'incohérence est réelle et la valeur attendue est correcte
- INVALIDE : la correction est erronée, mal formulée, ou la valeur attendue est fausse
- INCERTAIN : impossible de trancher sans contexte supplémentaire

Réponds UNIQUEMENT avec un JSON valide :
{"verdict": "VALIDE"|"INVALIDE"|"INCERTAIN", "confidence": 0.0-1.0, "reasoning": "..."}"""

_JUDGE_TEMPLATE = """Phase advisor : {phase}

Incohérence signalée :
- Description : {description}
- Valeur observée : {observed}
- Valeur attendue : {expected}
- Rationale : {rationale}
- Contexte besoin : {context_need}
- Contexte matériel : {context_hw}

Cette correction est-elle valide ?"""


@dataclass
class JudgeVerdict:
    model: str
    verdict: Literal["VALIDE", "INVALIDE", "INCERTAIN"]
    confidence: float
    reasoning: str


@dataclass
class ConsensusResult:
    sonnet: JudgeVerdict
    haiku: JudgeVerdict
    consensus: Literal["VALIDE", "INVALIDE", "INCERTAIN", "DISAGREEMENT"]
    final_confidence: float
    auto_actionable: bool  # True if both agree and action can be taken without human


def _judge_one(model: str, correction: dict) -> JudgeVerdict:
    import anthropic
    client = anthropic.AnthropicBedrock()
    prompt = _JUDGE_TEMPLATE.format(
        phase=correction.get("phase", ""),
        description=correction.get("description", ""),
        observed=correction.get("observed_value") or "non précisé",
        expected=correction.get("expected_value") or "non précisé",
        rationale=correction.get("rationale") or "non précisé",
        context_need=correction.get("context_need") or "non précisé",
        context_hw=correction.get("context_hw") or "non précisé",
    )
    response = client.messages.create(
        model=model,
        max_tokens=512,
        system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    try:
        data = json.loads(raw)
        return JudgeVerdict(
            model=model,
            verdict=data.get("verdict", "INCERTAIN"),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", ""),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        return JudgeVerdict(model=model, verdict="INCERTAIN", confidence=0.3, reasoning=raw[:200])


def evaluate_correction(correction: dict) -> ConsensusResult:
    """Run Sonnet + Haiku in parallel and compute consensus."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_sonnet = pool.submit(_judge_one, _SONNET, correction)
        f_haiku  = pool.submit(_judge_one, _HAIKU,  correction)
        sonnet = f_sonnet.result()
        haiku  = f_haiku.result()

    if sonnet.verdict == haiku.verdict and sonnet.verdict != "INCERTAIN":
        consensus = sonnet.verdict
        final_confidence = (sonnet.confidence + haiku.confidence) / 2
        auto_actionable = True
    elif sonnet.verdict == "INCERTAIN" or haiku.verdict == "INCERTAIN":
        consensus = "INCERTAIN"
        final_confidence = 0.4
        auto_actionable = False
    else:
        consensus = "DISAGREEMENT"
        final_confidence = 0.5
        auto_actionable = False

    return ConsensusResult(
        sonnet=sonnet,
        haiku=haiku,
        consensus=consensus,
        final_confidence=final_confidence,
        auto_actionable=auto_actionable,
    )


def run_judge_pipeline(correction_id: str) -> dict:
    """Evaluate a correction and apply the result if consensus is reached.

    Returns a summary dict with the action taken.
    """
    from pipeline.utils.memory_store import get_correction, validate_correction, reject_correction

    correction = get_correction(correction_id)
    if not correction:
        return {"error": "correction not found", "id": correction_id}
    if correction["status"] != "staging":
        return {"error": "correction not in staging", "id": correction_id, "status": correction["status"]}

    try:
        result = evaluate_correction(correction)
    except Exception as e:
        logger.error("LLM judge failed for %s: %s", correction_id, e)
        return {"error": str(e), "id": correction_id, "action": "none"}

    action = "none"
    if result.auto_actionable:
        if result.consensus == "VALIDE":
            validate_correction(correction_id, validated_by="llm_consensus")
            action = "validated"
        elif result.consensus == "INVALIDE":
            reason = f"LLM consensus INVALIDE — Sonnet: {result.sonnet.reasoning[:100]} | Haiku: {result.haiku.reasoning[:100]}"
            reject_correction(correction_id, reason=reason)
            action = "rejected"

    return {
        "id": correction_id,
        "action": action,
        "consensus": result.consensus,
        "confidence": result.final_confidence,
        "auto_actionable": result.auto_actionable,
        "sonnet": {"verdict": result.sonnet.verdict, "confidence": result.sonnet.confidence},
        "haiku":  {"verdict": result.haiku.verdict,  "confidence": result.haiku.confidence},
    }


def run_judge_pipeline_batch(limit: int = 20) -> list[dict]:
    """Process all staging corrections that haven't been judged yet."""
    from pipeline.utils.memory_store import list_corrections
    staging = list_corrections(status="staging", limit=limit)
    # Only process human-submitted corrections (not already llm_consensus)
    to_judge = [c for c in staging if c.get("source") == "human"]
    return [run_judge_pipeline(c["id"]) for c in to_judge]
