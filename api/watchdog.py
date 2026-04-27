"""Background watchdog: detect stuck training runs and mark them failed."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from api.db import execute, fetchall

logger = logging.getLogger("mlops.watchdog")

_STUCK_THRESHOLD_MINUTES = 120  # runs stuck > 2h are considered dead


async def watchdog_loop(interval_seconds: int = 60) -> None:
    """Periodic task: scan running runs and fail stuck ones."""
    while True:
        try:
            _check_stuck_runs()
        except Exception as exc:
            logger.error("Watchdog error: %s", exc)
        await asyncio.sleep(interval_seconds)


def _check_stuck_runs() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=_STUCK_THRESHOLD_MINUTES)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    stuck = fetchall(
        "SELECT id, started_at FROM runs WHERE status = 'running' AND started_at < ?",
        (cutoff,),
    )
    for run in stuck:
        execute(
            "UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), run["id"]),
        )
        logger.warning("Watchdog: marked run %s as failed (started %s, threshold %dm)",
                       run["id"], run["started_at"], _STUCK_THRESHOLD_MINUTES)
