"""LLM policy: what may leave the machine for a given project (config/llm_policy.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY_PATH = Path("config/llm_policy.yaml")
PROJECTS_DIR = Path("config/projects")

_DENY = {"cloud": "deny", "audit_required": True}


class LLMPolicyError(RuntimeError):
    """A call was blocked by the LLM policy (or could not be audited). Must reach a human."""


@dataclass(frozen=True)
class PolicyDecision:
    project_id: str | None
    level: str                       # classification level, or "unscoped"
    cloud: str                       # allow | anonymize | deny
    audit_required: bool
    sensitive_terms: list[str] = field(default_factory=list)
    reason: str = ""                 # why cloud is denied, when it is
    store_sent_text: str = "anonymized"


def _policy_path() -> Path:
    return Path(os.environ.get("MLOPS_LLM_POLICY", DEFAULT_POLICY_PATH))


def _load_policy() -> dict[str, Any]:
    path = _policy_path()
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _project_config(project_id: str) -> dict[str, Any] | None:
    path = Path(os.environ.get("MLOPS_PROJECTS_DIR", PROJECTS_DIR)) / f"{project_id}.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def resolve(project_id: str | None) -> PolicyDecision:
    """Decide what a call for this project may do. Anything unknown denies cloud (fail closed)."""
    policy = _load_policy()
    levels: dict[str, Any] = policy.get("levels") or {}
    store = str((policy.get("audit") or {}).get("store_sent_text", "anonymized"))

    if not levels:
        return PolicyDecision(project_id, "unknown", "deny", True, reason="policy file missing or empty", store_sent_text=store)

    if project_id is None:
        level = str(policy.get("unscoped_level", ""))
        cfg: dict[str, Any] = {}
        label = "unscoped"
    else:
        project = _project_config(project_id)
        if project is None:
            return PolicyDecision(project_id, "unknown", "deny", True, reason=f"project config not found: {project_id}", store_sent_text=store)
        level = str(project.get("classification", ""))
        cfg = project
        label = level

    rules = levels.get(level)
    if rules is None:
        return PolicyDecision(project_id, label or "unknown", "deny", True, reason=f"unknown classification level {level!r}", store_sent_text=store)

    cloud = str(rules.get("cloud", "deny"))
    audit_required = bool(rules.get("audit_required", False))
    terms = cfg.get("sensitive_terms")
    reason = ""
    if cloud == "anonymize" and rules.get("require_sensitive_terms") and terms is None:
        cloud, reason = "deny", "`sensitive_terms` is not defined in the project config (an empty list is an explicit choice)"
    elif cloud == "deny":
        reason = f"cloud LLMs are not allowed for level {level!r}"
    return PolicyDecision(
        project_id, label, cloud, audit_required, [str(t) for t in (terms or [])], reason, store,
    )
