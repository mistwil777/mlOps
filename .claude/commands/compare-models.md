# /compare-models — Comparative table of N models

## Usage
```
/compare-models project=<name> [models=run1,run2,run3] [include-baseline=true]
```

## What this skill does
Queries MLflow for all eval runs of a project and generates a side-by-side
comparison table in HTML + terminal output.

If `models` is not specified: compares all runs with completed eval for the project.

## Steps for Claude

### Step 1 — Query MLflow for runs
```bash
source .venv/bin/activate && python -m pipeline.evaluate list-runs \
    --experiment <project> \
    --with-metrics java_compilation,codebLEU,latency_p50,exact_match
```

### Step 2 — Generate comparison
```bash
python -m pipeline.evaluate compare \
    --project <project> \
    --runs <run_ids or "all"> \
    --include-baseline <true/false> \
    --output dashboards/compare_<project>_$(date +%Y%m%d).html
```

### Step 3 — Identify best model
Ranking criteria (in order of priority):
1. Java compilation rate (highest first)
2. CodeBLEU (highest first)
3. Latency p50 (lowest first)

Highlight the best model per metric and the overall winner.

## Expected output
```
Model comparison: migrator (4 runs + nova-pro baseline)

Run          | Iters | Compilation | CodeBLEU | Latency p50 | ExactMatch
─────────────────────────────────────────────────────────────────────────
nova-pro     | —     | 97.0%       | 0.763    | 1840ms      | 18.0%
abc123       | 1000  | 94.0%  ✓   | 0.731    | 142ms  ✓    | 12.0%
def456       | 2000  | 95.5%      | 0.748  ✓ | 158ms       | 14.0%
ghi789       | 500   | 88.0%      | 0.694    | 128ms       | 9.0%

Best overall: def456 (2000 iters)
Best latency: ghi789 (but below compilation gate)

Dashboard: dashboards/compare_migrator_20260423.html
```
