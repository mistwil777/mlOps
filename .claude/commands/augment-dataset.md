<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /augment-dataset — Generate synthetic training pairs

## Usage
```
/augment-dataset project=<name> n=<count> strategy=<paraphrase|back-translate|mutate> [model=local]
```

## What this skill does
Generates `n` synthetic pairs from existing validated pairs to increase dataset size.
Uses a local LLM only (Ollama or mlx_lm.server) — zero cloud calls.
Writes to `datalake/projects/<project>/processed/pairs_synthetic.jsonl`.

## When to use
- Dataset < 500 pairs after cleaning
- Covering edge cases under-represented in natural data
- Specific code patterns you want the model to learn better

## Strategies

### paraphrase
Rewrites the `prompt` part in different ways while keeping the `completion` identical.
Increases diversity of input phrasing without touching ground truth.

### back-translate
Generates intermediate representation then back to source language.
Useful for code: Python → pseudocode → Python (different style, same logic).

### mutate
Takes existing pairs and applies controlled mutations:
- Rename variables (consistent within the pair)
- Change loop style (for→while, list comprehension→explicit)
- Add/remove type annotations

## Steps for Claude

### Step 1 — Validate clean data exists
Check `datalake/projects/<project>/fine-tuning-ready/train.jsonl` exists and has content.

### Step 2 — Check Ollama is running
```bash
curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; models=[m['name'] for m in json.load(sys.stdin)['models']]; print('Available:', models)"
```
If Ollama is not running: `ollama serve &` and wait 2 seconds.

### Step 3 — Run augmentation
```bash
source .venv/bin/activate && python -m agents.quality_scorer augment \
    --project <project> \
    --n <count> \
    --strategy <strategy>
```

### Step 4 — Quality gate
All synthetic pairs go through the same cleaning pipeline:
```bash
python -m pipeline.clean --input pairs_synthetic.jsonl --project <project> --score true --threshold 0.75
```
Higher threshold (0.75 vs 0.7) for synthetic data — be more selective.

### Step 5 — Merge with train set
```bash
python -m pipeline.utils.jsonl merge \
    datalake/projects/<project>/fine-tuning-ready/train.jsonl \
    datalake/projects/<project>/processed/pairs_synthetic_clean.jsonl \
    --output datalake/projects/<project>/fine-tuning-ready/train_augmented.jsonl \
    --deduplicate
```

## Expected output
```
Generated: 200 synthetic pairs (strategy: paraphrase)
After quality gate: 167 pairs accepted (threshold: 0.75)
Merged with train set: 628 + 167 = 795 pairs

Output: datalake/projects/<project>/fine-tuning-ready/train_augmented.jsonl
```
