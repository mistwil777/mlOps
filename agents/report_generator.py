"""Report generator agent: daily dashboard and watch reports."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, date
from pathlib import Path
from typing import Any


def _read_audit_log(target_date: date, log_path: Path) -> dict[str, int]:
    counts = {"bash": 0, "write": 0, "total": 0}
    if not log_path.exists():
        return counts

    date_prefix = target_date.isoformat()
    with log_path.open() as fh:
        for line in fh:
            if not line.startswith(date_prefix):
                continue
            counts["total"] += 1
            if "| BASH |" in line:
                counts["bash"] += 1
            elif "| WRITE |" in line:
                counts["write"] += 1
    return counts


def _query_mlflow_runs(target_date: date) -> list[dict[str, Any]]:
    """Query MLflow tracking server for runs on target_date."""
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
    try:
        import mlflow  # noqa: PLC0415
        mlflow.set_tracking_uri(tracking_uri)
        client = mlflow.tracking.MlflowClient()
        experiments = client.search_experiments()
        runs = []
        for exp in experiments:
            exp_runs = client.search_runs(
                experiment_ids=[exp.experiment_id],
                filter_string=f"attributes.start_time >= {int(datetime.combine(target_date, datetime.min.time()).timestamp()) * 1000}",
            )
            for run in exp_runs:
                runs.append({
                    "run_id": run.info.run_id[:8],
                    "experiment": exp.name,
                    "status": run.info.status,
                    "duration_s": (
                        (run.info.end_time - run.info.start_time) / 1000
                        if run.info.end_time else None
                    ),
                    "metrics": dict(run.data.metrics),
                })
        return runs
    except Exception:  # noqa: BLE001
        return []


def _check_disk_usage(path: Path) -> float:
    """Return disk usage percentage for the partition containing path."""
    import shutil  # noqa: PLC0415
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100


def _dataset_sizes(projects_root: Path) -> dict[str, dict[str, int]]:
    sizes = {}
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        train = project_dir / "fine-tuning-ready" / "train.jsonl"
        valid = project_dir / "fine-tuning-ready" / "valid.jsonl"
        sizes[project_dir.name] = {
            "train": sum(1 for _ in train.open()) if train.exists() else 0,
            "valid": sum(1 for _ in valid.open()) if valid.exists() else 0,
        }
    return sizes


def generate_daily_report(target_date: date | None = None, output: Path | None = None) -> Path:
    target_date = target_date or date.today()
    output = output or Path(f"dashboards/daily_{target_date:%Y%m%d}.html")
    output.parent.mkdir(parents=True, exist_ok=True)

    base = Path(__file__).parents[1]
    audit = _read_audit_log(target_date, base / ".claude" / "audit.log")
    mlflow_runs = _query_mlflow_runs(target_date)
    disk_pct = _check_disk_usage(base)

    try:
        dataset_sizes = _dataset_sizes(base / "datalake" / "projects")
    except Exception:  # noqa: BLE001
        dataset_sizes = {}

    alerts = []
    if disk_pct > 80:
        alerts.append(f"Disk usage critical: {disk_pct:.1f}%")

    run_rows = "".join(
        f"<tr><td>{r['run_id']}</td><td>{r['experiment']}</td>"
        f"<td>{r['status']}</td>"
        f"<td>{r['duration_s']:.0f}s</td>"
        f"<td>{r['metrics'].get('val_loss', 'N/A')}</td></tr>"
        for r in mlflow_runs
    ) or "<tr><td colspan=5>No runs today</td></tr>"

    dataset_rows = "".join(
        f"<tr><td>{proj}</td><td>{v['train']}</td><td>{v['valid']}</td></tr>"
        for proj, v in dataset_sizes.items()
    ) or "<tr><td colspan=3>No datasets</td></tr>"

    alerts_html = "".join(f"<p style='color:red'>⚠ {a}</p>" for a in alerts) or "<p style='color:green'>No alerts</p>"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>MLOps Daily Report — {target_date}</title>
<meta http-equiv="refresh" content="300">
<style>
body{{font-family:monospace;margin:2em;background:#f5f5f5}}
h1{{color:#333}} h2{{color:#555;border-bottom:1px solid #ccc}}
table{{border-collapse:collapse;width:100%;background:white}}
td,th{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#eee}} .card{{background:white;padding:1em;margin:1em 0;border-radius:4px}}
</style></head><body>
<h1>MLOps Daily Report — {target_date}</h1>
<p>Generated: {datetime.utcnow().isoformat()}Z</p>

<div class="card">
<h2>Activity</h2>
<p>CC actions: {audit['total']} (bash: {audit['bash']}, write: {audit['write']})</p>
<p>Disk usage: {disk_pct:.1f}%</p>
</div>

<div class="card">
<h2>Training Runs</h2>
<table><tr><th>Run ID</th><th>Experiment</th><th>Status</th><th>Duration</th><th>Val Loss</th></tr>
{run_rows}</table>
</div>

<div class="card">
<h2>Datasets</h2>
<table><tr><th>Project</th><th>Train</th><th>Valid</th></tr>
{dataset_rows}</table>
</div>

<div class="card">
<h2>Alerts</h2>
{alerts_html}
</div>

</body></html>"""

    output.write_text(html, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    daily = subparsers.add_parser("daily")
    daily.add_argument("--date", default=None)
    daily.add_argument("--projects", default="all")
    daily.add_argument("--output", default=None)

    args = parser.parse_args()

    if args.command == "daily":
        target = date.fromisoformat(args.date) if args.date else date.today()
        out = generate_daily_report(target, Path(args.output) if args.output else None)
        print(f"Report written to {out}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
