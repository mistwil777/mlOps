"""Shared LLM generation backend with automatic fallback chain.

Priority order (first available wins):
  1. AWS Bedrock — model from AWS_BEDROCK_MODEL_GENERATION env var
  2. Ollama      — model from OLLAMA_GEN_MODEL env var (local, blocked on corp network)

Usage:
    from pipeline.utils.llm import call_llm, current_backend

    code = call_llm("Translate this Python to Java: ...", max_tokens=2048)
"""

from __future__ import annotations

import os
from typing import Any

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.llm")

_BEDROCK_MODEL_DEFAULT = "us.anthropic.claude-sonnet-4-6"
_OLLAMA_URL            = "http://localhost:11434/api/generate"
_OLLAMA_MODEL_DEFAULT  = "qwen2.5-coder:7b"
_HTTP_TIMEOUT          = 120

_BACKEND: str = ""


def _call_bedrock(prompt: str, max_tokens: int, temperature: float) -> str:
    import boto3

    region   = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    model_id = os.environ.get("AWS_BEDROCK_MODEL_GENERATION", _BEDROCK_MODEL_DEFAULT)

    from botocore.config import Config as _Config  # noqa: PLC0415
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=_Config(connect_timeout=10, read_timeout=120, retries={"max_attempts": 2}),
    )

    # Use Converse API — works for all Bedrock models (Claude, Nova Pro, etc.)
    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    return response["output"]["message"]["content"][0]["text"].strip()


def _call_ollama(prompt: str, max_tokens: int, temperature: float) -> str:
    import httpx

    model = os.environ.get("OLLAMA_GEN_MODEL", _OLLAMA_MODEL_DEFAULT)
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


_BACKENDS: list[tuple[str, Any]] = [
    ("bedrock", _call_bedrock),
    ("ollama",  _call_ollama),
]


def call_llm(
    prompt: str,
    max_tokens: int = 2048,
    temperature: float = 0.1,
) -> str | None:
    """Call the best available LLM backend. Returns None if all backends fail."""
    global _BACKEND  # noqa: PLW0603

    start_idx = 0
    if _BACKEND:
        for i, (name, _) in enumerate(_BACKENDS):
            if name == _BACKEND:
                start_idx = i
                break

    for name, fn in _BACKENDS[start_idx:]:
        try:
            result = fn(prompt, max_tokens, temperature)
            if _BACKEND != name:
                logger.info("LLM backend: %s", name)
                _BACKEND = name
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM backend '%s' unavailable: %s — trying next", name, exc)
            if _BACKEND == name:
                _BACKEND = ""

    logger.error("All LLM backends failed for prompt: %s…", prompt[:80])
    return None


def current_backend() -> str:
    return _BACKEND


def reset_backend() -> None:
    global _BACKEND  # noqa: PLW0603
    _BACKEND = ""
