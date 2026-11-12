"""Celery tasks — clean_raw and generate_pairs pipeline steps."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from api.celery_app import celery_app


def _run_subprocess(self: Any, cmd: list[str]) -> dict[str, Any]:
    """Run cmd as subprocess, stream output lines into Celery task state."""
    lines: list[str] = []
    self.update_state(state="PROGRESS", meta={"lines": lines, "returncode": None})

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:  # type: ignore[union-attr]
            stripped = line.rstrip()
            lines.append(stripped)
            # Push state every 5 lines to avoid flooding Redis
            if len(lines) % 5 == 0:
                self.update_state(state="PROGRESS", meta={"lines": lines[:], "returncode": None})

        proc.wait()
        return {"lines": lines, "returncode": proc.returncode}
    except Exception as exc:  # noqa: BLE001
        lines.append(f"ERROR: {exc}")
        return {"lines": lines, "returncode": -1}


@celery_app.task(
    bind=True,
    name="mlops.clean_raw",
    max_retries=0,
    soft_time_limit=1800,
    time_limit=1860,
)
def clean_raw(
    self: Any,
    project_id: str,
    adapter: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run pipeline.clean_raw as a Celery task."""
    cmd = [sys.executable, "-m", "pipeline.clean_raw", "--project", project_id]
    if adapter:
        cmd += ["--adapter", adapter]
    if dry_run:
        cmd.append("--dry-run")

    result = _run_subprocess(self, cmd)

    if result["returncode"] == 0:
        # Trigger datalake index (non-blocking best-effort)
        try:
            from pipeline.datalake_index import DatalakeIndex  # noqa: PLC0415
            idx = DatalakeIndex()
            idx.index_project(project_id, stage="raw")
        except Exception:  # noqa: BLE001
            pass

    return result


@celery_app.task(
    bind=True,
    name="mlops.generate_pairs",
    max_retries=0,
    soft_time_limit=3600,
    time_limit=3660,
)
def generate_pairs(
    self: Any,
    project_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run agents.pair_generator as a Celery task."""
    cmd = [sys.executable, "-m", "agents.pair_generator", "--project", project_id]
    if dry_run:
        cmd.append("--dry-run")

    result = _run_subprocess(self, cmd)

    if result["returncode"] == 0:
        try:
            from pipeline.datalake_index import DatalakeIndex  # noqa: PLC0415
            idx = DatalakeIndex()
            idx.index_project(project_id, stage="fine-tuning-ready")
        except Exception:  # noqa: BLE001
            pass

    return result
