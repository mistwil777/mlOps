"""The ledger must survive a restart: a second instance on the same file sees everything."""

import threading

from pipeline.utils.run_ledger import TERMINAL_STATUSES, RunLedger


def test_restart_loses_nothing(tmp_path) -> None:
    db = tmp_path / "ledger.db"
    first = RunLedger(db)
    first.create_job("j1", "projA", "fuse", meta={"filename": "a.pdf"})
    first.log("j1", "step 1")
    first.log("j1", "step 2", level="warning")
    first.set_fields("j1", returncode=0, result={"saved": 3})
    first.set_status("j1", "completed")
    first.close()

    second = RunLedger(db)  # simulates an API restart
    job = second.get_job("j1")
    assert job["status"] == "completed"
    assert job["finished_at"] is not None
    assert job["returncode"] == 0
    assert job["result"] == {"saved": 3}
    assert job["meta"] == {"filename": "a.pdf"}
    assert job["n_lines"] == 2
    assert second.lines("j1") == ["step 1", "step 2"]


def test_events_since_streams_by_seq(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "l.db")
    ledger.create_job("j", "p", "t")
    ledger.log("j", "a")
    first = ledger.events_since("j")
    ledger.log("j", "b")
    ledger.log("j", "c")
    later = ledger.events_since("j", first[-1][0])
    assert [m for _, m in first] == ["a"]
    assert [m for _, m in later] == ["b", "c"]


def test_terminal_status_is_final(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "l.db")
    ledger.create_job("j", "p", "t")
    assert ledger.set_status("j", "cancelled") is True
    assert ledger.set_status("j", "completed") is False  # a cancelled job stays cancelled
    assert ledger.get_job("j")["status"] == "cancelled"


def test_unknown_job_is_handled(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "l.db")
    assert ledger.exists("nope") is False
    assert ledger.get_job("nope") is None
    assert ledger.set_status("nope", "failed") is False


def test_find_running_by_project_and_type(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "l.db")
    ledger.create_job("a", "p1", "clean")
    ledger.create_job("b", "p2", "clean")
    ledger.set_status("b", "completed")
    assert ledger.find_running("p1", "clean") == "a"
    assert ledger.find_running("p2", "clean") is None
    assert ledger.find_running("p1", "other") is None


def test_orphans_become_interrupted_only_for_the_api_executor(tmp_path) -> None:
    db = tmp_path / "l.db"
    ledger = RunLedger(db)
    ledger.create_job("api-run", "p", "t", executor="api")
    ledger.create_job("api-done", "p", "t", executor="api")
    ledger.set_status("api-done", "completed")
    ledger.create_job("celery-run", "p", "t", executor="celery")
    ledger.close()

    restarted = RunLedger(db)
    assert restarted.interrupt_orphans("api") == 1
    assert restarted.get_job("api-run")["status"] == "interrupted"
    assert "interrupted" in restarted.lines("api-run")[-1]
    assert restarted.get_job("api-done")["status"] == "completed"
    assert restarted.get_job("celery-run")["status"] == "running"  # worker may still be alive
    assert "interrupted" in TERMINAL_STATUSES


def test_concurrent_logging_keeps_every_line(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "l.db")
    ledger.create_job("j", "p", "t")

    def worker(tag: str) -> None:
        for i in range(100):
            ledger.log("j", f"{tag}-{i}")

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = ledger.lines("j")
    assert len(lines) == 400
    for n in range(4):  # per-thread order is preserved
        own = [m for m in lines if m.startswith(f"t{n}-")]
        assert own == [f"t{n}-{i}" for i in range(100)]


def test_default_path_parent_is_created(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "missing" / "dir" / "mlops.db")
    ledger.create_job("j", "p", "t")
    assert ledger.exists("j")


def test_list_jobs_filters_by_project(tmp_path) -> None:
    ledger = RunLedger(tmp_path / "l.db")
    ledger.create_job("a", "p1", "t")
    ledger.create_job("b", "p2", "t")
    assert [j["id"] for j in ledger.list_jobs("p1")] == ["a"]
    assert {j["id"] for j in ledger.list_jobs()} == {"a", "b"}
