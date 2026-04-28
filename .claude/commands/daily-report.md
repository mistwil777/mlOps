<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /daily-report — Aggregated daily report

## Usage
```
/daily-report [date=YYYY-MM-DD] [projects=all|<name1>,<name2>]
```

## What this skill does
Generates a comprehensive HTML dashboard summarizing all activity for the day.
Pure automation — reads logs, MLflow, SQLite. Zero AI call needed.

## Steps for Claude

### Step 1 — Collect data
```bash
source .venv/bin/activate && python -m agents.report_generator daily \
    --date <date or today> \
    --projects <projects> \
    --output dashboards/daily_$(date +%Y%m%d).html
```

Data sources:
- `.claude/audit.log` — all CC actions today
- MLflow API — runs started/completed today
- `experiments/mlops.db` — project and dataset stats
- `dashboards/*.jsonl` — eval results
- System metrics (RAM, disk usage)

### Step 2 — Report sections

**Activity summary**
- N CC commands run
- N files written/modified
- N Bash commands executed

**Training**
- Runs started today (with status: running/completed/failed)
- Best metric improvement vs yesterday
- Compute time used (hours)

**Data**
- N new pairs added per project
- Dataset sizes (train/valid per project)
- Last cleaning run date

**Models**
- Currently deployed model per project
- Last eval scores
- Serving status (up/down, latency)

**Alerts** (highlighted in red)
- Models below quality thresholds
- Datasets not updated in > 30 days
- Disk usage > 80%
- MLflow not reachable

### Step 3 — Open in browser (optional)
```bash
open dashboards/daily_$(date +%Y%m%d).html
```

## Expected output
```
Daily report generated: dashboards/daily_20260423.html

Summary:
  Projects active: 1 (migrator)
  Training runs today: 1 (completed)
  New pairs added: 89 (web collection)
  Models deployed: 1 (migrator → abc123)
  Alerts: 0

Open: open dashboards/daily_20260423.html
```

## Scheduling
```bash
# Run automatically at 8am every day
python -m agents.report_generator schedule --cron "0 8 * * *"
```
