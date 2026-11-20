"""Celery application — broker: Redis, backend: Redis."""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import task_failure, task_postrun, task_prerun

from pipeline.utils.ledger_client import final_status, report_job, result_for_report

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")

celery_app = Celery(
    "mlops",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=[
        "api.tasks.indexation",
        "api.tasks.pipeline",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Paris",
    enable_utc=True,
    task_track_started=True,
    result_expires=86400,          # résultats conservés 24h
    worker_prefetch_multiplier=1,  # 1 tâche à la fois par worker → backpressure propre
    task_acks_late=True,           # acquittement après succès, pas au dépôt
)


# Job ledger: the worker reports to the API (single writer of the SQLite file), best-effort.
@task_prerun.connect
def _ledger_task_started(task_id: str | None = None, **_: object) -> None:
    report_job(task_id, status="running")


@task_failure.connect
def _ledger_task_failed(task_id: str | None = None, exception: BaseException | None = None, **_: object) -> None:
    report_job(task_id, lines=[f"ERROR: {exception!r}"], level="error")


@task_postrun.connect
def _ledger_task_finished(
    task_id: str | None = None, retval: object = None, state: str | None = None, **_: object
) -> None:
    code = retval.get("returncode") if isinstance(retval, dict) else None
    report_job(task_id, status=final_status(state, retval), returncode=code, result=result_for_report(retval))
