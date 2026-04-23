# /eval-model — Complete model evaluation + baseline comparison

## Usage
```
/eval-model project=<name> adapter=<adapter_path> [baseline=nova-pro|local:<model_path>] [n=50]
```

## What this skill does
Runs the evaluation suite on a fine-tuned adapter against a test set,
compares with baseline, and generates an HTML dashboard.

## Steps for Claude

### Step 1 — Prepare test set
```bash
# Use the validation set as test set (no data leakage)
wc -l datalake/projects/<project>/fine-tuning-ready/valid.jsonl
```
If `n` < total valid pairs: sample randomly (deterministic seed).

### Step 2 — Run inference on fine-tuned model
```bash
python -m pipeline.evaluate run \
    --project <project> \
    --adapter <adapter_path> \
    --test datalake/projects/<project>/fine-tuning-ready/valid.jsonl \
    --n <n> \
    --output experiments/eval_<project>_$(date +%Y%m%d_%H%M%S).json
```

Metrics computed:
- **Java compilation rate** — each output compiled with `javac` in sandbox (target: ≥ 0.90)
- **CodeBLEU** — weighted ngram + AST + dataflow similarity (target: ≥ 0.70)
- **Exact match rate** — identical to reference
- **Token length ratio** — output tokens / reference tokens
- **Latency p50/p95** — inference time per request

### Step 3 — Run baseline comparison (if requested)

#### If baseline=nova-pro:
```bash
python -m pipeline.evaluate baseline-nova \
    --project <project> \
    --test datalake/projects/<project>/fine-tuning-ready/valid.jsonl \
    --n <n>
```
Uses AWS Bedrock credentials from `.env`.

#### If baseline=local:<model_path>:
```bash
python -m pipeline.evaluate run \
    --project <project> \
    --model <model_path> \
    --test datalake/projects/<project>/fine-tuning-ready/valid.jsonl \
    --n <n>
```

### Step 4 — Generate comparison report
```bash
python -m pipeline.evaluate report \
    --project <project> \
    --results experiments/eval_<project>_<timestamp>.json \
    --output dashboards/eval_<project>_<timestamp>.html
```

### Step 5 — Quality gate check
Read thresholds from `config/projects/<project>.yaml`:
- `min_java_compilation` (default 0.90)
- `min_codebLEU` (default 0.70)
- `max_latency_ratio` (default 2.0 vs baseline)

Print PASS/FAIL for each threshold.
If any threshold fails: print specific recommendation (more data, more iters, etc.).

### Step 6 — Log to MLflow
```bash
python -m pipeline.evaluate log-run \
    --run-id <run_id_from_training>
```

## Expected output
```
Evaluation: mistral-migrator vs nova-pro baseline (n=50)

Metric              Fine-tuned    Nova Pro    Delta    Gate
─────────────────────────────────────────────────────────────
Java compilation    94.0%         97.0%       -3.0%    ✓ PASS (≥90%)
CodeBLEU            0.731         0.763       -0.032   ✓ PASS (≥0.70)
Exact match         12.0%         18.0%       -6.0%    (no gate)
Latency p50         142ms         1840ms      -92.3%   ✓ PASS (≤2× baseline)
Latency p95         287ms         3120ms      -90.8%   ✓ PASS

Overall: ✓ ALL GATES PASSED — model ready for deployment

Dashboard: dashboards/eval_migrator_20260423_143022.html
MLflow: http://localhost:5001/#/experiments/1/runs/abc123

Next: /deploy-model project=migrator adapter=adapters/migrator/abc123/
```
