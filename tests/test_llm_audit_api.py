"""Workers record LLM calls through the token-protected API; the audit is consultable."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pipeline.utils import run_ledger  # noqa: E402

CALL = {
    "id": "c1", "ts": 1.0, "project_id": "p", "level": "confidentiel", "purpose": "t",
    "decision": "anonymized", "backend": "bedrock", "model": "m", "entities_json": '{"TERM": 1}',
    "sent_sha256": "abc", "sent_chars": 12, "sent_text": "hello [TERM_1]",
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ledger, "DEFAULT_DB", tmp_path / "mlops.db")
    monkeypatch.setenv("MLOPS_INTERNAL_TOKEN", "tok")
    run_ledger._LEDGERS.clear()
    from api.routers import llm_audit

    app = FastAPI()
    app.include_router(llm_audit.router)
    yield TestClient(app)
    run_ledger._LEDGERS.clear()


def _h(token="tok"):
    return {"X-Internal-Token": token}


def test_record_outcome_and_list(api) -> None:
    assert api.post("/api/llm-calls", json=CALL, headers=_h()).status_code == 200
    assert api.post("/api/llm-calls/c1/outcome", json={"ok": True, "latency_ms": 42, "response_chars": 7}, headers=_h()).status_code == 200
    (row,) = api.get("/api/llm-calls", params={"project_id": "p"}).json()
    assert (row["decision"], row["ok"], row["latency_ms"], row["entities"]) == ("anonymized", 1, 42, {"TERM": 1})
    assert row["sent_text"] == "hello [TERM_1]"
    assert api.get("/api/llm-calls", params={"project_id": "other"}).json() == []


def test_writes_require_the_token(api, monkeypatch) -> None:
    assert api.post("/api/llm-calls", json=CALL, headers=_h("bad")).status_code == 403
    assert api.post("/api/llm-calls", json=CALL).status_code == 403
    assert api.post("/api/llm-calls/c1/outcome", json={"ok": True}, headers=_h("bad")).status_code == 403
    assert api.get("/api/llm-calls").json() == []
    monkeypatch.delenv("MLOPS_INTERNAL_TOKEN")
    assert api.post("/api/llm-calls", json=CALL, headers=_h()).status_code == 503
