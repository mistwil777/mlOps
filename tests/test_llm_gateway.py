"""The gateway enforces the policy on every call: spies prove which backend saw which text."""

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from pipeline.utils import llm, llm_audit, run_ledger
from pipeline.utils.llm_policy import LLMPolicyError

POLICY = {
    "levels": {
        "interne": {"cloud": "allow", "audit_required": False},
        "confidentiel": {"cloud": "anonymize", "audit_required": True, "require_sensitive_terms": True},
        "secret": {"cloud": "deny", "audit_required": True},
    },
    "unscoped_level": "interne",
    "audit": {"store_sent_text": "anonymized"},
}
PROJECTS = {
    "open": {"classification": "interne"},
    "conf": {"classification": "confidentiel", "sensitive_terms": ["ORION", "Acme"]},
    "conf-no-terms": {"classification": "confidentiel"},
    "sec": {"classification": "secret"},
}
PROMPT = "Rapport du projet ORION chez Acme, contact paul@acme.fr : résume."


class Spy:
    def __init__(self, reply="ok", fail=False):
        self.calls, self.reply, self.fail = [], reply, fail

    def __call__(self, prompt, max_tokens, temperature, model_id=None):
        self.calls.append(prompt)
        if self.fail:
            raise ConnectionError("down")
        return self.reply(prompt) if callable(self.reply) else self.reply


@pytest.fixture
def gw(tmp_path, monkeypatch):
    (tmp_path / "projects").mkdir()
    for name, cfg in PROJECTS.items():
        (tmp_path / "projects" / f"{name}.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "policy.yaml").write_text(yaml.safe_dump(POLICY), encoding="utf-8")
    monkeypatch.setenv("MLOPS_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("MLOPS_LLM_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.delenv("MLOPS_API_URL", raising=False)
    monkeypatch.setattr(run_ledger, "DEFAULT_DB", tmp_path / "audit.db")
    run_ledger._LEDGERS.clear()
    llm.reset_backend()
    spies = {"bedrock": Spy("cloud-answer"), "ollama": Spy("local-answer"), "mlx": Spy("mlx-answer")}
    monkeypatch.setattr(llm, "_FUNCS", dict(spies))
    yield spies
    run_ledger._LEDGERS.clear()
    llm.reset_backend()


def _audit(project_id=None):
    return run_ledger.get_ledger().list_llm_calls(project_id)


# ── interne: cloud allowed ────────────────────────────────────────────────────

def test_interne_goes_to_the_cloud_and_is_audited(gw) -> None:
    assert llm.call_llm(PROMPT, project_id="open", purpose="t") == "cloud-answer"
    assert gw["bedrock"].calls == [PROMPT]
    (row,) = _audit("open")
    assert (row["decision"], row["backend"], row["level"], row["ok"]) == ("allowed", "bedrock", "interne", 1)
    assert row["sent_text"] is None and row["sent_sha256"] == hashlib.sha256(PROMPT.encode()).hexdigest()


# ── secret: never cloud, even right after a cloud success (sticky backend trap) ─

def test_secret_never_reaches_bedrock_even_after_a_cloud_success(gw) -> None:
    assert llm.call_llm("hello", project_id="open") == "cloud-answer"
    assert llm.current_backend() == "bedrock"
    bedrock_calls_before = len(gw["bedrock"].calls)

    assert llm.call_llm(PROMPT, project_id="sec", purpose="t") == "local-answer"
    assert len(gw["bedrock"].calls) == bedrock_calls_before  # the sticky backend did not leak
    assert gw["ollama"].calls == [PROMPT]
    (row,) = _audit("sec")
    assert (row["decision"], row["backend"], row["level"]) == ("local", "ollama", "secret")


def test_secret_with_no_local_backend_is_blocked_not_degraded(gw) -> None:
    gw["ollama"].fail = gw["mlx"].fail = True
    with pytest.raises(LLMPolicyError) as exc:
        llm.call_llm(PROMPT, project_id="sec", purpose="t")
    assert "secret" in str(exc.value) and "Bloqué" in str(exc.value)
    assert gw["bedrock"].calls == []
    assert "denied" in {r["decision"] for r in _audit("sec")}


def test_secret_falls_back_to_the_second_local_backend(gw) -> None:
    gw["ollama"].fail = True  # Ollama blocked on the corporate network
    assert llm.call_llm(PROMPT, project_id="sec") == "mlx-answer"
    assert gw["bedrock"].calls == []


def test_unknown_project_is_treated_as_denied(gw) -> None:
    assert llm.call_llm(PROMPT, project_id="ghost") == "local-answer"
    assert gw["bedrock"].calls == []


# ── confidentiel: anonymized cloud ────────────────────────────────────────────

def test_confidentiel_sends_only_anonymized_text_and_restores_the_answer(gw) -> None:
    gw["bedrock"].reply = lambda sent: f"Résumé : {sent}"  # the model echoes what it received
    answer = llm.call_llm(PROMPT, project_id="conf", purpose="t")
    (sent,) = gw["bedrock"].calls
    for secret in ("ORION", "Acme", "paul", "acme.fr"):
        assert secret not in sent
    assert answer == f"Résumé : {PROMPT}"  # restored exactly, locally
    (row,) = _audit("conf")
    assert row["decision"] == "anonymized" and row["sent_text"] == sent
    assert row["sent_sha256"] == hashlib.sha256(sent.encode()).hexdigest()
    assert row["entities"] == {"EMAIL": 1, "TERM": 2}
    assert "ORION" not in json.dumps(row)


def test_confidentiel_falls_back_to_local_with_the_original_text(gw) -> None:
    gw["bedrock"].fail = True
    assert llm.call_llm(PROMPT, project_id="conf") == "local-answer"
    assert gw["ollama"].calls == [PROMPT]  # local backends need no anonymization


def test_confidentiel_without_sensitive_terms_never_reaches_the_cloud(gw) -> None:
    assert llm.call_llm(PROMPT, project_id="conf-no-terms") == "local-answer"
    assert gw["bedrock"].calls == []


def test_a_call_that_cannot_be_audited_is_not_sent_for_confidentiel(gw, monkeypatch) -> None:
    monkeypatch.setattr(llm_audit, "start", lambda call: False)
    with pytest.raises(LLMPolicyError) as exc:
        llm.call_llm(PROMPT, project_id="conf")
    assert "audit" in str(exc.value)
    assert gw["bedrock"].calls == [] and gw["ollama"].calls == []


def test_a_call_that_cannot_be_audited_still_goes_out_for_interne(gw, monkeypatch) -> None:
    monkeypatch.setattr(llm_audit, "start", lambda call: False)
    assert llm.call_llm(PROMPT, project_id="open") == "cloud-answer"


# ── explicit provider, unscoped ───────────────────────────────────────────────

def test_explicit_cloud_provider_is_never_rerouted_for_secret(gw) -> None:
    config = {"provider": "bedrock", "model_id": "some-model"}
    with pytest.raises(LLMPolicyError):
        llm.call_llm_with_config(PROMPT, config, project_id="sec", purpose="t")
    assert gw["bedrock"].calls == [] and gw["ollama"].calls == []


def test_explicit_cloud_provider_is_anonymized_for_confidentiel(gw) -> None:
    llm.call_llm_with_config(PROMPT, {"provider": "bedrock", "model_id": "m"}, project_id="conf")
    assert "ORION" not in gw["bedrock"].calls[0]


def test_unknown_provider_falls_back_with_the_same_project(gw) -> None:
    assert llm.call_llm_with_config(PROMPT, {"provider": "mystery"}, project_id="sec") == "local-answer"
    assert gw["bedrock"].calls == []


def test_unscoped_calls_are_audited_as_unscoped(gw) -> None:
    assert llm.call_llm("hello", purpose="legacy") == "cloud-answer"
    (row,) = _audit()
    assert (row["level"], row["project_id"], row["purpose"]) == ("unscoped", None, "legacy")


def test_cloud_model_override_reaches_bedrock_only(gw, monkeypatch) -> None:
    seen = {}
    gw["bedrock"] = lambda prompt, mt, t, model_id=None: seen.setdefault("model", model_id) or "ok"
    monkeypatch.setitem(llm._FUNCS, "bedrock", gw["bedrock"])
    llm.call_llm("x", project_id="open", cloud_model="us.amazon.nova-pro-v1:0")
    assert seen["model"] == "us.amazon.nova-pro-v1:0"


# ── worker containers audit through the API, never through the SQLite file ────

class _Recorder(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Recorder.received.append((self.path, self.headers.get("X-Internal-Token"), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass


def test_a_worker_audits_through_the_api_and_never_opens_the_database(gw, monkeypatch, tmp_path) -> None:
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MLOPS_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MLOPS_INTERNAL_TOKEN", "tok")
    try:
        assert llm.call_llm(PROMPT, project_id="conf") == "cloud-answer"
    finally:
        server.shutdown()
    paths = [p for p, _, _ in _Recorder.received]
    assert paths[0] == "/api/llm-calls" and paths[1].endswith("/outcome")
    assert all(token == "tok" for _, token, _ in _Recorder.received)
    assert _Recorder.received[0][2]["decision"] == "anonymized"
    assert not (tmp_path / "audit.db").exists()  # the worker never touched the SQLite file


def test_a_worker_without_a_token_cannot_audit_so_confidentiel_is_blocked(gw, monkeypatch) -> None:
    monkeypatch.setenv("MLOPS_API_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("MLOPS_INTERNAL_TOKEN", raising=False)
    with pytest.raises(LLMPolicyError):
        llm.call_llm(PROMPT, project_id="conf")
    assert gw["bedrock"].calls == []
