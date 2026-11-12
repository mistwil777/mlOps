"""Celery application — broker: Redis, backend: Redis."""

from __future__ import annotations

import os

from celery import Celery

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
