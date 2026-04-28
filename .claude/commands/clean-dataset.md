<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /clean-dataset — Full cleaning pipeline for a training dataset

## Usage
```
/clean-dataset project=<name> [score=true|false] [threshold=0.7]
```

## What this skill does
Runs the full cleaning pipeline on `datalake/projects/<project>/processed/pairs_raw.jsonl`
and writes clean data to `datalake/projects/<project>/fine-tuning-ready/train.jsonl`
and `datalake/projects/<project>/fine-tuning-ready/valid.jsonl`.

Uses `pipeline/clean.py::DataCleaner` — no AI call unless `score=true`.
If `score=true`: uses a local LLM (Ollama) to score pairs — no cloud call.

## Steps for Claude

### Step 1 — Load and validate input
```bash
python -m pipeline.utils.jsonl validate \
    datalake/projects/<project>/processed/pairs_raw.jsonl \
    --required-keys prompt,completion
```

### Step 2 — Run cleaning pipeline
```bash
source .venv/bin/activate && python -m pipeline.clean \
    --project <project> \
    --score <score> \
    --threshold <threshold>
```

Pipeline stages (in order):
1. **Exact deduplication** — hash-based, O(n)
2. **MinHash deduplication** — near-duplicate detection (jaccard ≥ 0.85)
3. **Rule-based filtering** — applies thresholds from `config/projects/<project>.yaml`:
   - min/max token length
   - input/output ratio (reject if output < 0.3× or > 10× input)
   - blacklist patterns (passwords, secrets, TODO markers)
4. **Quality scoring** (if `score=true`) — Ollama local LLM rates each pair 0-1
5. **Train/valid split** — 90/10, stratified by token length bucket

### Step 3 — Generate quality report
```bash
python -m pipeline.clean report \
    --project <project> \
    --output dashboards/quality_<project>_$(date +%Y%m%d).html
```

### Step 4 — Log to MLflow
```python
python -m pipeline.clean log-run \
    --project <project> \
    --experiment <project>
```
Logs: n_raw, n_clean, n_train, n_valid, dedup_removed, filtered_removed, mean_quality_score.

## Expected output
```
Input:  847 pairs
After dedup:     801 pairs (-46 duplicates)
After filtering: 743 pairs (-58 filtered)
After scoring:   698 pairs (-45 below threshold 0.7)

Train: 628 pairs → datalake/projects/<project>/fine-tuning-ready/train.jsonl
Valid: 70 pairs  → datalake/projects/<project>/fine-tuning-ready/valid.jsonl

Quality report: dashboards/quality_<project>_20260423.html
MLflow run: http://localhost:5001/#/experiments/1/runs/<run_id>

Next: /train-lora project=<project>
```
