"""Celery jobs reach the ledger through the API (single writer): endpoints, worker client, enqueue, cancel."""

import json
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pipeline.utils import ledger_client, run_ledger  # noqa: E402

TOKEN = "s3cret"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(run_ledger, "DEFAULT_DB", tmp_path / "mlops.db")
    run_ledger._LEDGERS.clear()
    yield run_ledger.get_ledger()
    run_ledger._LEDGERS.clear()


@pytest.fixture
def client(ledger, monkeypatch):
    monkeypatch.setenv("MLOPS_INTERNAL_TOKEN", TOKEN)
    from api.routers import jobs

    app = FastAPI()
    app.include_router(jobs.router)
    return TestClient(app)


def _post(client, job_id, body, token=TOKEN):
    headers = {"X-Internal-Token": token} if token is not None else {}
    return client.post(f"/api/jobs/{job_id}/report", json=body, headers=headers)


# ── API: report endpoint ──────────────────────────────────────────────────────

def test_report_is_disabled_without_server_token(ledger, monkeypatch) -> None:
    monkeypatch.delenv("MLOPS_INTERNAL_TOKEN", raising=False)
    from api.routers import jobs

    app = FastAPI()
    app.include_router(jobs.router)
    ledger.create_job("j", "p", "t", status="pending", executor="celery")
    assert _post(TestClient(app), "j", {"status": "running"}).status_code == 503


def test_report_rejects_wrong_or_missing_token(client, ledger) -> None:
    ledger.create_job("j", "p", "t", status="pending", executor="celery")
    assert _post(client, "j", {"status": "running"}, token="nope").status_code == 403
    assert _post(client, "j", {"status": "running"}, token=None).status_code == 403
    assert ledger.get_job("j")["status"] == "pending"


def test_report_records_lines_result_and_status_in_order(client, ledger) -> None:
    ledger.create_job("j", "p", "clean-raw", status="pending", executor="celery")
    assert _post(client, "j", {"status": "running"}).status_code == 200
    assert _post(client, "j", {"lines": ["a", "b"]}).status_code == 200
    assert _post(client, "j", {"lines": ["c"], "status": "completed", "returncode": 0, "result": {"n": 3}}).status_code == 200
    job = ledger.get_job("j")
    assert (job["status"], job["returncode"], job["result"]) == ("completed", 0, {"n": 3})
    assert ledger.lines("j") == ["a", "b", "c"]


def test_report_validations(client, ledger) -> None:
    ledger.create_job("j", "p", "t", status="running", executor="celery")
    assert _post(client, "j", {"status": "cancelled"}).status_code == 422  # only the API cancels
    assert _post(client, "missing", {"status": "running"}).status_code == 404


def test_a_late_worker_report_cannot_revive_a_cancelled_job(client, ledger) -> None:
    ledger.create_job("j", "p", "t", status="running", executor="celery")
    ledger.set_status("j", "cancelled")
    _post(client, "j", {"status": "completed"})
    assert ledger.get_job("j")["status"] == "cancelled"


# ── API: history endpoints ────────────────────────────────────────────────────

def test_history_endpoints(client, ledger) -> None:
    ledger.create_job("a", "p1", "clean-raw")
    ledger.create_job("b", "p2", "fuse")
    ledger.log("a", "hello")
    assert [j["id"] for j in client.get("/api/jobs", params={"project_id": "p1"}).json()] == ["a"]
    assert client.get("/api/jobs/a").json()["n_lines"] == 1
    events = client.get("/api/jobs/a/events").json()["events"]
    assert [e["line"] for e in events] == ["hello"]
    assert client.get("/api/jobs/a/events", params={"since": events[-1]["seq"]}).json()["events"] == []
    assert client.get("/api/jobs/zzz").status_code == 404


# ── Worker client ─────────────────────────────────────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Recorder.received.append((self.path, self.headers.get("X-Internal-Token"), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def api_server(monkeypatch):
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MLOPS_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MLOPS_INTERNAL_TOKEN", TOKEN)
    yield _Recorder
    server.shutdown()


def test_client_posts_the_report_with_the_token(api_server) -> None:
    assert ledger_client.report_job("j1", lines=["x"], status="running") is True
    path, token, body = api_server.received[0]
    assert path == "/api/jobs/j1/report" and token == TOKEN
    assert body["lines"] == ["x"] and body["status"] == "running"


def test_client_is_silent_when_disabled_or_unreachable(monkeypatch) -> None:
    monkeypatch.delenv("MLOPS_API_URL", raising=False)
    assert ledger_client.report_job("j", status="running") is False
    monkeypatch.setenv("MLOPS_API_URL", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setenv("MLOPS_INTERNAL_TOKEN", TOKEN)
    assert ledger_client.report_job("j", status="running") is False  # never raises


def test_line_batcher_sends_batches_and_flushes_the_rest(api_server) -> None:
    batcher = ledger_client.LineBatcher("j", size=3, interval_s=3600)
    for i in range(7):
        batcher.add(f"l{i}")
    batcher.flush()
    sent = [b["lines"] for _, _, b in api_server.received]
    assert sent == [["l0", "l1", "l2"], ["l3", "l4", "l5"], ["l6"]]


def test_final_status_and_result_helpers() -> None:
    assert ledger_client.final_status("SUCCESS", {"returncode": 0}) == "completed"
    assert ledger_client.final_status("SUCCESS", {"returncode": 2}) == "failed"
    assert ledger_client.final_status("SUCCESS", {"saved": 3}) == "completed"
    assert ledger_client.final_status("FAILURE", None) == "failed"
    assert ledger_client.result_for_report({"lines": ["x"], "returncode": 0}) == {"returncode": 0}
    assert ledger_client.result_for_report({"lines": ["x"]}) is None
    assert ledger_client.result_for_report("not a dict") is None


# ── API: enqueue and cancel (Celery stubbed) ──────────────────────────────────

class _FakeTask:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def apply_async(self, args, task_id):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append((args, task_id))


@pytest.fixture
def data_client(ledger, monkeypatch):
    revoked = []
    fake_celery = types.SimpleNamespace(control=types.SimpleNamespace(revoke=lambda *a, **k: revoked.append((a, k))))
    monkeypatch.setitem(sys.modules, "api.celery_app", types.SimpleNamespace(celery_app=fake_celery))
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "qdrant_client", types.SimpleNamespace(QdrantClient=object))
    from api.routers import data

    monkeypatch.setattr(data, "_load_project_config", lambda project_id: {})
    app = FastAPI()
    app.include_router(data.router)
    return TestClient(app), data, revoked


def test_enqueue_records_the_job_and_reuses_its_id_as_task_id(data_client, ledger) -> None:
    _, data, _ = data_client
    task = _FakeTask()
    job_id = data._enqueue(task, "clean-raw", "projA", "projA", None, False)
    assert task.calls == [(("projA", None, False), job_id)]
    job = ledger.get_job(job_id)
    assert (job["status"], job["executor"], job["job_type"], job["project_id"]) == ("pending", "celery", "clean-raw", "projA")


def test_enqueue_marks_the_job_failed_when_the_broker_is_down(data_client, ledger) -> None:
    from fastapi import HTTPException

    _, data, _ = data_client
    with pytest.raises(HTTPException) as exc:
        data._enqueue(_FakeTask(fail=True), "clean-raw", "projA", "projA")
    assert exc.value.status_code == 503
    job = ledger.list_jobs("projA")[0]
    assert job["status"] == "failed" and "queue unavailable" in ledger.lines(job["id"])[0]


def test_cancel_revokes_the_celery_task_and_marks_the_job(data_client, ledger) -> None:
    client, _, revoked = data_client
    ledger.create_job("j", "p", "clean-raw", status="running", executor="celery")
    assert client.delete("/api/data/clean-raw/j").json() == {"status": "cancelled", "job_id": "j"}
    assert revoked and revoked[0][0] == ("j",) and revoked[0][1]["terminate"] is True
    assert ledger.get_job("j")["status"] == "cancelled"
    assert client.delete("/api/data/clean-raw/j").json()["message"] == "Job already finished"
    assert client.delete("/api/data/clean-raw/none").status_code == 404


# ── Celery signals (eager mode: the real task lifecycle, no broker needed) ────

def test_celery_signals_report_the_task_lifecycle(api_server) -> None:
    pytest.importorskip("celery")
    from api.celery_app import celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False  # the failure is recorded, not raised

    @celery_app.task(name="test.job_reporting.ok")
    def ok():
        return {"returncode": 0}

    @celery_app.task(name="test.job_reporting.bad_rc")
    def bad_rc():
        return {"returncode": 3, "lines": ["raw output stays out of the report"]}

    @celery_app.task(name="test.job_reporting.boom")
    def boom():
        raise RuntimeError("kaput")

    ok.apply_async(task_id="t-ok")
    bad_rc.apply_async(task_id="t-rc")
    boom.apply_async(task_id="t-boom")

    def reports(job_id):
        return [body for path, _, body in api_server.received if path == f"/api/jobs/{job_id}/report"]

    assert [r["status"] for r in reports("t-ok") if r["status"]] == ["running", "completed"]
    assert reports("t-ok")[-1]["returncode"] == 0

    assert [r["status"] for r in reports("t-rc") if r["status"]] == ["running", "failed"]
    assert reports("t-rc")[-1]["returncode"] == 3
    assert reports("t-rc")[-1]["result"] == {"returncode": 3}  # the raw output lines are not copied

    boom_reports = reports("t-boom")
    assert any("kaput" in line for r in boom_reports for line in r["lines"])
    assert [r["status"] for r in boom_reports if r["status"]] == ["running", "failed"]


# ── Reconciliation with Celery's own state (worker could not report) ──────────

@pytest.mark.parametrize(
    "celery_state, retval, expected",
    [
        ("SUCCESS", {"returncode": 0}, "completed"),
        ("SUCCESS", {"returncode": 2}, "failed"),
        ("FAILURE", None, "failed"),
        ("REVOKED", None, "cancelled"),
        ("STARTED", None, "running"),
        ("PENDING", None, "pending"),
    ],
)
def test_celery_jobs_are_aligned_with_celery_state_on_read(client, ledger, monkeypatch, celery_state, retval, expected) -> None:
    ledger.create_job("j", "p", "clean-raw", status="pending", executor="celery")
    fake = types.SimpleNamespace(
        celery_app=types.SimpleNamespace(AsyncResult=lambda _id: types.SimpleNamespace(state=celery_state, result=retval))
    )
    monkeypatch.setitem(sys.modules, "api.celery_app", fake)
    assert client.get("/api/jobs/j").json()["status"] == expected
    assert ledger.get_job("j")["status"] == expected


def test_reconcile_keeps_the_ledger_status_when_the_broker_is_unreachable(client, ledger, monkeypatch) -> None:
    ledger.create_job("j", "p", "clean-raw", status="running", executor="celery")

    def _down(_id):
        raise ConnectionError("redis down")

    fake = types.SimpleNamespace(celery_app=types.SimpleNamespace(AsyncResult=_down))
    monkeypatch.setitem(sys.modules, "api.celery_app", fake)
    assert client.get("/api/jobs/j").json()["status"] == "running"


def test_api_jobs_are_never_reconciled_with_celery(client, ledger, monkeypatch) -> None:
    ledger.create_job("j", "p", "fuse", status="running", executor="api")
    monkeypatch.setitem(sys.modules, "api.celery_app", types.SimpleNamespace())  # would raise if touched
    assert client.get("/api/jobs/j").json()["status"] == "running"
