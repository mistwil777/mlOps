"""Unified logger for the MLOps pipeline."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_LOGS_DIR = Path(__file__).parents[2] / "logs"
_LOGS_DIR.mkdir(exist_ok=True)

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_MLFLOW_RUN_ID: str | None = None


def _build_formatter() -> logging.Formatter:
    return logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def get_logger(name: str) -> logging.Logger:
    """Return a logger writing to console and a date-stamped file."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, _LOG_LEVEL, logging.INFO))
    formatter = _build_formatter()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    log_file = _LOGS_DIR / f"{datetime.utcnow():%Y-%m-%d}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


def set_mlflow_run(run_id: str) -> None:
    """Register the active MLflow run so log_metric() knows where to push."""
    global _MLFLOW_RUN_ID
    _MLFLOW_RUN_ID = run_id


def log_metric(key: str, value: float, step: int | None = None) -> None:
    """Push a metric to MLflow if a run is active; always logs locally."""
    logger = get_logger("mlops.metrics")
    logger.info("metric | %s=%s step=%s run=%s", key, value, step, _MLFLOW_RUN_ID)

    if _MLFLOW_RUN_ID is None:
        return

    try:
        import mlflow  # noqa: PLC0415

        with mlflow.start_run(run_id=_MLFLOW_RUN_ID):
            mlflow.log_metric(key, value, step=step)
    except Exception as exc:  # noqa: BLE001
        logger.warning("MLflow metric push failed: %s", exc)


def log_params(params: dict[str, Any], run_id: str | None = None) -> None:
    """Push a dict of parameters to MLflow."""
    active_run = run_id or _MLFLOW_RUN_ID
    if active_run is None:
        return

    try:
        import mlflow  # noqa: PLC0415

        with mlflow.start_run(run_id=active_run):
            mlflow.log_params(params)
    except Exception as exc:  # noqa: BLE001
        get_logger("mlops.params").warning("MLflow params push failed: %s", exc)
