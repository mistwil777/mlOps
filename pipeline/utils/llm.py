"""The single gateway to text LLMs: every call is checked against the project's policy and audited.

What may leave the machine depends on the project's classification (config/llm_policy.yaml):
  allow      -> cloud (AWS Bedrock) then local backends
  anonymize  -> cloud receives anonymized text only (response restored locally), then local backends
  deny       -> local backends only (ollama, mlx); if none is reachable the call is BLOCKED (LLMPolicyError)

The decision is recorded in the audit trail BEFORE the prompt is sent. Pass `project_id` on every call:
a call without it is audited as "unscoped" and counted by tests/test_no_direct_llm_calls.py.

Usage:
    from pipeline.utils.llm import call_llm

    text = call_llm("Summarize ...", project_id="my-project", purpose="summary")
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from pipeline.utils import llm_audit
from pipeline.utils.anonymizer import Anonymizer
from pipeline.utils.llm_policy import LLMPolicyError, PolicyDecision, resolve
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.llm")

_BEDROCK_MODEL_DEFAULT = "us.anthropic.claude-sonnet-4-6"
_OLLAMA_URL            = "http://localhost:11434/api/generate"
_OLLAMA_MODEL_DEFAULT  = "qwen2.5-coder:7b"
_MLX_URL_DEFAULT       = "http://localhost:8080"
_HTTP_TIMEOUT          = 120

_CLOUD_BACKENDS = frozenset({"bedrock"})
_LAST_OK: dict[str, str] = {}   # policy mode ("cloud" | "local") -> last backend that worked
_LAST_USED: str = ""

__all__ = ["LLMPolicyError", "call_llm", "call_llm_with_config", "current_backend", "reset_backend"]


# ── Backends ──────────────────────────────────────────────────────────────────

def _call_bedrock(prompt: str, max_tokens: int, temperature: float, model_id: str | None = None) -> str:
    import boto3
    from botocore.config import Config as _Config  # noqa: PLC0415

    region   = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    model_id = model_id or os.environ.get("AWS_BEDROCK_MODEL_GENERATION", _BEDROCK_MODEL_DEFAULT)
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=_Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 2}),
    )
    # Converse API works for all Bedrock models (Claude, Nova Pro, etc.)
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    return response["output"]["message"]["content"][0]["text"].strip()


def _call_ollama(prompt: str, max_tokens: int, temperature: float, model_id: str | None = None) -> str:
    import httpx

    model = model_id or os.environ.get("OLLAMA_GEN_MODEL", _OLLAMA_MODEL_DEFAULT)
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        r = client.post(
            _OLLAMA_URL,
            json={
                "model":   model,
                "prompt":  prompt,
                "stream":  False,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            },
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()


def _call_mlx(prompt: str, max_tokens: int, temperature: float, model_id: str | None = None) -> str:
    """Local mlx_lm.server (OpenAI-compatible): usable when Ollama is blocked by the corporate network."""
    import httpx

    base = os.environ.get("MLX_SERVER_URL", _MLX_URL_DEFAULT).rstrip("/")
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        r = client.post(
            f"{base}/v1/chat/completions",
            json={
                "model": model_id or "local",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


# Looked up by name at call time (tests replace entries with spies).
_FUNCS: dict[str, Any] = {"bedrock": _call_bedrock, "ollama": _call_ollama, "mlx": _call_mlx}
_ORDER = ("bedrock", "ollama", "mlx")


# ── Gateway ───────────────────────────────────────────────────────────────────

def _chain(decision: PolicyDecision) -> tuple[str, list[str]]:
    """Backends allowed by the policy, in order. Rebuilt on every call: no state leaks between projects."""
    if decision.cloud in ("allow", "anonymize"):
        return "cloud", list(_ORDER)
    return "local", [b for b in _ORDER if b not in _CLOUD_BACKENDS]


def _audit_start(
    decision: PolicyDecision, purpose: str, backend: str, model: str | None,
    sent: str, entities: dict[str, int], kind: str, anonymized: bool,
) -> str | None:
    """Record the call BEFORE sending. Returns the call id, or None when no record is needed/possible."""
    if kind == "local" and not decision.audit_required:
        return None
    call_id = uuid.uuid4().hex
    keep_text = anonymized and decision.store_sent_text == "anonymized"
    recorded = llm_audit.start({
        "id": call_id, "ts": time.time(), "project_id": decision.project_id, "level": decision.level,
        "purpose": purpose, "decision": "anonymized" if anonymized else ("allowed" if kind == "cloud" else "local"),
        "backend": backend, "model": model, "entities_json": json.dumps(entities) if entities else None,
        "sent_sha256": hashlib.sha256(sent.encode("utf-8")).hexdigest(), "sent_chars": len(sent),
        "sent_text": sent if keep_text else None,
    })
    if not recorded and decision.audit_required:
        raise LLMPolicyError(
            f"Appel bloqué (niveau {decision.level}) : l'audit de l'appel est impossible, rien n'est envoyé."
        )
    return call_id if recorded else None


def _record_denial(decision: PolicyDecision, purpose: str, why: str) -> None:
    llm_audit.start({
        "id": uuid.uuid4().hex, "ts": time.time(), "project_id": decision.project_id, "level": decision.level,
        "purpose": purpose, "decision": "denied", "backend": None, "model": None, "entities_json": None,
        "sent_sha256": None, "sent_chars": 0, "sent_text": None,
    })
    logger.warning("LLM call denied (project=%s level=%s purpose=%s): %s", decision.project_id, decision.level, purpose, why)


def _denial_message(decision: PolicyDecision, why: str) -> str:
    return f"Bloqué par la politique LLM (niveau {decision.level}) : {why}"


def _run_chain(
    prompt: str, backends: list[str], mode: str, decision: PolicyDecision, purpose: str,
    max_tokens: int, temperature: float, models: dict[str, str | None],
) -> str | None:
    global _LAST_USED  # noqa: PLW0603

    start = backends.index(_LAST_OK[mode]) if _LAST_OK.get(mode) in backends else 0
    anonymized_prompt = None
    for name in backends[start:]:
        kind = "cloud" if name in _CLOUD_BACKENDS else "local"
        send, mapping, entities, anonymize = prompt, {}, {}, False
        if kind == "cloud" and decision.cloud == "anonymize":
            if anonymized_prompt is None:
                anonymized_prompt = Anonymizer(decision.sensitive_terms).anonymize(prompt)
            send, mapping, entities, anonymize = (
                anonymized_prompt.text, anonymized_prompt.mapping, anonymized_prompt.counts, True,
            )
        call_id = _audit_start(decision, purpose, name, models.get(name), send, entities, kind, anonymize)
        started = time.perf_counter()
        try:
            result = _FUNCS[name](send, max_tokens, temperature, models.get(name))
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM backend '%s' unavailable: %s — trying next", name, exc)
            if call_id:
                llm_audit.finish(call_id, ok=False, latency_ms=int((time.perf_counter() - started) * 1000), error=str(exc)[:300])
            if _LAST_OK.get(mode) == name:
                _LAST_OK.pop(mode, None)
            continue
        if call_id:
            llm_audit.finish(call_id, ok=True, latency_ms=int((time.perf_counter() - started) * 1000), response_chars=len(result))
        if _LAST_OK.get(mode) != name:
            logger.info("LLM backend: %s", name)
        _LAST_OK[mode] = _LAST_USED = name
        return Anonymizer.restore(result, mapping) if mapping else result
    return None


def call_llm(
    prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    *,
    project_id: str | None = None,
    purpose: str = "",
    cloud_model: str | None = None,
) -> str | None:
    """Call the best backend the project's policy allows. None when every allowed backend failed.

    Raises LLMPolicyError when cloud is forbidden and no local backend answered (or the call cannot be audited).
    """
    decision = resolve(project_id)
    mode, backends = _chain(decision)
    result = _run_chain(
        prompt, backends, mode, decision, purpose, max_tokens, temperature, {"bedrock": cloud_model},
    )
    if result is None and decision.cloud == "deny":
        message = _denial_message(decision, f"{decision.reason}, et aucun backend local n'est joignable")
        _record_denial(decision, purpose, message)
        raise LLMPolicyError(message)
    if result is None:
        logger.error("All LLM backends failed for prompt: %s…", prompt[:80])
    return result


def call_llm_with_config(
    prompt: str,
    llm_config: dict,
    max_tokens: int = 2048,
    *,
    project_id: str | None = None,
    purpose: str = "",
) -> str | None:
    """Call the backend named in llm_config (keys: provider bedrock|ollama, model_id, temperature).

    An explicit cloud provider is never silently rerouted: a project that forbids cloud gets LLMPolicyError.
    Falls back to call_llm() (same project) if the provider is unknown.
    """
    provider    = llm_config.get("provider", "bedrock")
    model_id    = llm_config.get("model_id", "")
    temperature = float(llm_config.get("temperature", 0.1))

    if provider in ("bedrock", "ollama") and model_id:
        decision = resolve(project_id)
        if provider in _CLOUD_BACKENDS and decision.cloud == "deny":
            message = _denial_message(decision, decision.reason)
            _record_denial(decision, purpose, message)
            raise LLMPolicyError(message)
        mode = "cloud" if provider in _CLOUD_BACKENDS else "local"
        return _run_chain(prompt, [provider], mode, decision, purpose, max_tokens, temperature, {provider: model_id})

    logger.warning("call_llm_with_config: provider '%s' without model_id, falling back to call_llm()", provider)
    return call_llm(prompt, max_tokens=max_tokens, temperature=temperature, project_id=project_id, purpose=purpose)


def current_backend() -> str:
    return _LAST_USED


def reset_backend() -> None:
    global _LAST_USED  # noqa: PLW0603
    _LAST_OK.clear()
    _LAST_USED = ""
