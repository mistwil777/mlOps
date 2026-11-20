"""The policy denies by default: unknown project, unknown level, missing policy, missing sensitive_terms."""

import yaml

from pipeline.utils import llm_policy

POLICY = {
    "levels": {
        "interne": {"cloud": "allow", "audit_required": False},
        "confidentiel": {"cloud": "anonymize", "audit_required": True, "require_sensitive_terms": True},
        "secret": {"cloud": "deny", "audit_required": True},
    },
    "unscoped_level": "interne",
    "audit": {"store_sent_text": "anonymized"},
}


def _setup(tmp_path, monkeypatch, projects: dict, policy: dict | None = POLICY) -> None:
    pdir = tmp_path / "projects"
    pdir.mkdir()
    for name, cfg in projects.items():
        (pdir / f"{name}.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    if policy is not None:
        (tmp_path / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    monkeypatch.setenv("MLOPS_PROJECTS_DIR", str(pdir))
    monkeypatch.setenv("MLOPS_LLM_POLICY", str(tmp_path / "policy.yaml"))


def test_levels_map_to_the_proposed_modes(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, {
        "a": {"classification": "interne"},
        "b": {"classification": "confidentiel", "sensitive_terms": ["ORION"]},
        "c": {"classification": "secret"},
    })
    assert llm_policy.resolve("a").cloud == "allow"
    b = llm_policy.resolve("b")
    assert (b.cloud, b.audit_required, b.sensitive_terms) == ("anonymize", True, ["ORION"])
    c = llm_policy.resolve("c")
    assert (c.cloud, c.audit_required) == ("deny", True) and "secret" in c.reason


def test_confidentiel_without_sensitive_terms_is_denied(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, {"b": {"classification": "confidentiel"}})
    decision = llm_policy.resolve("b")
    assert decision.cloud == "deny" and "sensitive_terms" in decision.reason


def test_an_empty_sensitive_terms_list_is_an_explicit_choice(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, {"b": {"classification": "confidentiel", "sensitive_terms": []}})
    assert llm_policy.resolve("b").cloud == "anonymize"


def test_unknown_project_unknown_level_and_missing_policy_deny(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, {"x": {"classification": "top-secret-plus"}, "y": {}})
    assert llm_policy.resolve("ghost").cloud == "deny"
    assert llm_policy.resolve("x").cloud == "deny"
    assert llm_policy.resolve("y").cloud == "deny"  # no classification at all
    monkeypatch.setenv("MLOPS_LLM_POLICY", str(tmp_path / "does-not-exist.yaml"))
    assert llm_policy.resolve(None).cloud == "deny"


def test_unscoped_calls_use_the_configured_unscoped_level(tmp_path, monkeypatch) -> None:
    _setup(tmp_path, monkeypatch, {})
    decision = llm_policy.resolve(None)
    assert decision.level == "unscoped" and decision.cloud == "allow"
