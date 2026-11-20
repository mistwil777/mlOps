"""The migrated call sites apply the project policy and show a block to the human."""

import json
import sys
import types
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pipeline.utils import llm, run_ledger  # noqa: E402
from pipeline.utils.llm_policy import LLMPolicyError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
POLICY = {
    "levels": {
        "interne": {"cloud": "allow", "audit_required": False},
        "secret": {"cloud": "deny", "audit_required": True},
    },
    "unscoped_level": "interne",
}
BAD_PAIR = {"prompt": "do x", "completion": "x", "adapter_name": "a"}  # completion too short -> audit flags it


class Spy:
    def __init__(self, reply="ok", fail=False):
        self.calls, self.reply, self.fail = [], reply, fail

    def __call__(self, prompt, max_tokens, temperature, model_id=None):
        self.calls.append(prompt)
        if self.fail:
            raise ConnectionError("down")
        return self.reply


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A working directory with two projects (interne, secret), a policy and one flaggable pair each."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config" / "projects").mkdir(parents=True)
    (tmp_path / "config" / "llm_policy.yaml").write_text(yaml.safe_dump(POLICY), encoding="utf-8")
    for pid, level in (("open", "interne"), ("sec", "secret")):
        (tmp_path / "config" / "projects" / f"{pid}.yaml").write_text(
            yaml.safe_dump({"classification": level, "domain": "legal"}), encoding="utf-8")
        ftr = tmp_path / "datalake" / "projects" / pid / "fine-tuning-ready"
        ftr.mkdir(parents=True)
        (ftr / "train.jsonl").write_text(json.dumps(BAD_PAIR) + "\n", encoding="utf-8")
    for var in ("MLOPS_PROJECTS_DIR", "MLOPS_LLM_POLICY", "MLOPS_API_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(run_ledger, "DEFAULT_DB", tmp_path / "audit.db")
    run_ledger._LEDGERS.clear()
    llm.reset_backend()
    spies = {"bedrock": Spy("cloud-recommendation"), "ollama": Spy("local-recommendation"), "mlx": Spy("mlx")}
    monkeypatch.setattr(llm, "_FUNCS", dict(spies))
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "qdrant_client", types.SimpleNamespace(QdrantClient=object))
    from api.routers import data

    app = FastAPI()
    app.include_router(data.router)
    yield TestClient(app), spies
    run_ledger._LEDGERS.clear()
    llm.reset_backend()


def test_ai_audit_uses_the_cloud_for_an_interne_project(world) -> None:
    client, spies = world
    item = client.get("/api/data/review/open/ai-audit").json()["suspicious"][0]
    assert item["recommendation"] == "cloud-recommendation"
    assert "open" in {c["project_id"] for c in run_ledger.get_ledger().list_llm_calls()}


def test_ai_audit_shows_a_policy_block_to_the_human_for_a_secret_project(world) -> None:
    client, spies = world
    spies["ollama"].fail = spies["mlx"].fail = True
    item = client.get("/api/data/review/sec/ai-audit").json()["suspicious"][0]
    assert item["recommendation"].startswith("Bloqué par la politique LLM (niveau secret)")
    assert spies["bedrock"].calls == []  # the client data never reached the cloud


def test_ai_audit_stays_local_for_a_secret_project(world) -> None:
    client, spies = world
    item = client.get("/api/data/review/sec/ai-audit").json()["suspicious"][0]
    assert item["recommendation"] == "local-recommendation" and spies["bedrock"].calls == []


def test_auto_correct_is_forbidden_when_the_policy_blocks_it(world) -> None:
    client, spies = world
    spies["ollama"].fail = spies["mlx"].fail = True
    response = client.post(
        "/api/data/review/sec/ai-audit/auto-correct",
        json={"index": 0, "source": "train", "reason": "empty_completion", "recommendation": "fix"},
    )
    assert response.status_code == 403 and "Bloqué par la politique" in response.json()["detail"]
    assert spies["bedrock"].calls == []


# ── agents ────────────────────────────────────────────────────────────────────

def test_semantic_judge_goes_through_the_gateway_with_the_project(monkeypatch) -> None:
    from agents import semantic_judge

    seen = {}

    def fake_call_llm(prompt, **kwargs):
        seen.update(kwargs)
        return "REASONING: same loop\nSCORE: 9"

    monkeypatch.setattr(semantic_judge, "call_llm", fake_call_llm)
    score, reasoning = semantic_judge.judge_pair("a", "b", "x", "y", project_id="sec")
    assert (score, reasoning) == (0.9, "same loop")
    assert seen["project_id"] == "sec" and seen["purpose"] == "semantic-judge"


def test_semantic_judge_never_turns_a_policy_block_into_a_score(monkeypatch) -> None:
    from agents import semantic_judge

    def blocked(prompt, **kwargs):
        raise LLMPolicyError("Bloqué par la politique LLM (niveau secret)")

    monkeypatch.setattr(semantic_judge, "call_llm", blocked)
    with pytest.raises(LLMPolicyError):
        semantic_judge.judge_pair("a", "b", project_id="sec")
    monkeypatch.setattr(semantic_judge, "call_llm", lambda prompt, **kw: None)
    assert semantic_judge.judge_pair("a", "b")[1].startswith("api_error")


def test_pair_fuser_passes_its_project_to_the_gateway(monkeypatch) -> None:
    from agents import pair_fuser

    seen = []
    monkeypatch.setattr(pair_fuser, "call_llm", lambda prompt, **kw: seen.append(kw) or None)
    fuser = pair_fuser.PairFuser("python", "java", project_id="sec")
    fuser.fuse([{"lang": "python", "code": "def f():\n    return 1\n"}], max_fused=1)
    assert seen and seen[0]["project_id"] == "sec" and seen[0]["purpose"] == "pair-fusion"


def test_pair_forge_default_llm_is_the_gateway(monkeypatch) -> None:
    # pair_forge.tools is not tracked in this repository (the package is incomplete in git): stub it.
    monkeypatch.syspath_prepend(str(ROOT / "agents" / "pair-forge"))
    monkeypatch.setitem(sys.modules, "pair_forge.tools", types.SimpleNamespace(
        apply_tools=None, check_dependencies=None, detect_language=None))
    agent_module = pytest.importorskip("pair_forge.agent")

    seen = {}
    monkeypatch.setattr(llm, "call_llm", lambda prompt, *a, **kw: seen.update(kw) or "out")
    monkeypatch.setattr(llm, "call_llm_with_config", lambda prompt, cfg, mt, **kw: seen.update(kw, cfg=cfg) or "cfg-out")
    gateway = agent_module._gateway_llm("sec")
    assert gateway("p", {}, 100) == "out" and seen["project_id"] == "sec"
    assert gateway("p", {"model_id": "m"}, 100) == "cfg-out" and seen["cfg"] == {"model_id": "m"}
