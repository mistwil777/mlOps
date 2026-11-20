"""Job routes read the ledger on disk: a job written by another process (a 'previous API') stays visible."""

import subprocess
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from pipeline.utils import run_ledger  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_WRITER = """
import sys, json
from pipeline.utils.run_ledger import RunLedger
db, job_id, job_type, status = sys.argv[1:5]
ledger = RunLedger(db)
ledger.create_job(job_id, "projA", job_type, meta={"filename": "doc.pdf"})
ledger.log(job_id, "line one")
ledger.log(job_id, "ERROR: boom", level="error")
if job_type == "ingest-raw":
    ledger.set_fields(job_id, result={"saved": 2})
if status != "running":
    ledger.set_status(job_id, status)
"""


def _stream_text(client: TestClient, url: str) -> str:
    """Read a finished SSE stream. sse_starlette keeps a module-level event bound to the first
    test event loop, so it is reset before each request (a test-only workaround)."""
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit_event = None
    return client.get(url).text


def _write_in_other_process(db: Path, job_id: str, job_type: str, status: str) -> None:
    """A separate process writes the job, then exits: nothing can be served from this process' memory."""
    subprocess.run([sys.executable, "-c", _WRITER, str(db), job_id, job_type, status], cwd=ROOT, check=True)


@pytest.fixture
def api(tmp_path, monkeypatch):
    db = tmp_path / "mlops.db"
    monkeypatch.setattr(run_ledger, "DEFAULT_DB", db)
    run_ledger._LEDGERS.clear()
    try:  # rag.py imports qdrant_client at module level; the job routes do not use it
        import qdrant_client  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, "qdrant_client", types.SimpleNamespace(QdrantClient=object))
    from api.routers import data, kpis, rag

    app = FastAPI()
    for router in (data.router, rag.router, kpis.router):
        app.include_router(router)
    yield TestClient(app), db
    run_ledger._LEDGERS.clear()


def test_status_and_stream_survive_a_restart(api) -> None:
    client, db = api
    _write_in_other_process(db, "j1", "fuse", "completed")

    status = client.get("/api/data/collect/j1/status").json()
    assert status == {"status": "completed", "lines": 2}

    body = _stream_text(client, "/api/data/collect/j1/stream")
    assert "line one" in body and "ERROR: boom" in body
    assert "event: done" in body and "completed" in body


def test_stream_ends_on_interrupted_jobs(api) -> None:
    client, db = api
    _write_in_other_process(db, "j2", "pipeline", "running")  # its process died while running
    assert run_ledger.get_ledger().interrupt_orphans("api") == 1

    body = _stream_text(client, "/api/data/collect/j2/stream")  # must terminate, not hang
    assert "event: done" in body and "interrupted" in body


def test_ingest_status_and_summary_stream(api) -> None:
    client, db = api
    _write_in_other_process(db, "ingest-raw-1", "ingest-raw", "completed")

    status = client.get("/api/data/ingest-raw/ingest-raw-1/status").json()
    assert status["status"] == "completed"
    assert status["filename"] == "doc.pdf"
    assert status["result"] == {"saved": 2}
    assert status["lines"] == ["line one", "ERROR: boom"]

    assert '[SUMMARY] {"saved": 2}' in _stream_text(client, "/api/data/ingest-raw/ingest-raw-1/stream")


def test_rag_build_stream_reads_the_ledger(api) -> None:
    client, db = api
    _write_in_other_process(db, "rag1", "rag-build", "failed")
    body = _stream_text(client, "/api/rag/build/rag1/stream")
    assert "line one" in body and "failed" in body


def test_kpis_failed_status_keeps_its_response_format(api) -> None:
    client, db = api
    _write_in_other_process(db, "k1", "robustness-eval", "failed")
    assert client.get("/api/kpis/jobs/k1").json() == {"job_id": "k1", "status": "failed: boom"}


def test_unknown_jobs_are_404(api) -> None:
    client, _ = api
    for url in (
        "/api/data/collect/nope/status",
        "/api/data/collect/nope/stream",
        "/api/data/ingest-raw/nope/status",
        "/api/rag/build/nope/stream",
        "/api/kpis/jobs/nope",
    ):
        assert client.get(url).status_code == 404, url
