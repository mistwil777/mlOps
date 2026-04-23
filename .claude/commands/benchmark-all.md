# /benchmark-all — Complete benchmarks across all versions

## Usage
```
/benchmark-all project=<name> [include-baseline=true] [n=100]
```

## What this skill does
Runs comprehensive benchmarks on all models ever deployed for a project,
producing a longitudinal performance report to track progress over time.

## Steps for Claude

### Step 1 — Discover all model versions
```bash
source .venv/bin/activate && python -m pipeline.evaluate list-models \
    --project <project>
```
Returns: list of (run_id, adapter_path, deployed_at, eval_status).

### Step 2 — Run eval on any model missing current eval
For each model without a recent eval (< 7 days old):
```bash
python -m pipeline.evaluate run \
    --project <project> \
    --adapter <adapter_path> \
    --test datalake/projects/<project>/fine-tuning-ready/valid.jsonl \
    --n <n>
```
Use Agent subtype haiku to run multiple evaluations in parallel.

### Step 3 — Run baseline if requested
```bash
python -m pipeline.evaluate baseline-nova \
    --project <project> \
    --test datalake/projects/<project>/fine-tuning-ready/valid.jsonl \
    --n <n>
```

### Step 4 — Generate full benchmark report
```bash
python -m pipeline.evaluate benchmark-report \
    --project <project> \
    --output dashboards/benchmark_<project>_$(date +%Y%m%d).html
```

Report includes:
- **Longitudinal chart**: metrics over time (by training date)
- **Cost/performance chart**: latency × quality tradeoff
- **Per-example analysis**: worst 10 failures across all models
- **Dataset drift**: how validation set has changed over time
- **Training efficiency**: val_loss curves comparison

### Step 5 — Regression check
Flag if current deployed model is worse than any previous model on any metric.

## Expected output
```
Benchmarking 3 models + nova-pro baseline (n=100)

Model        | Date       | Compilation | CodeBLEU | Latency
─────────────────────────────────────────────────────────────
nova-pro     | baseline   | 97.0%       | 0.763    | 1840ms
v1 (abc123)  | 2026-04-01 | 94.0%       | 0.731    | 142ms
v2 (def456)  | 2026-04-15 | 95.5%       | 0.748    | 158ms
v3 (ghi789)  | 2026-04-23 | 96.0% ✓    | 0.751 ✓  | 145ms ✓

Trend: ↑ improving across all metrics
No regression detected.

Dashboard: dashboards/benchmark_migrator_20260423.html
```
