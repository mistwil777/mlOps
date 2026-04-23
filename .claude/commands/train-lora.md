# /train-lora — Launch LoRA fine-tuning with optimal M4 Pro config

## Usage
```
/train-lora project=<name> [model=mistral-7b|qwen-14b] [iters=1000] [experiment=<name>] [resume=<adapter_path>]
```

## What this skill does
Launches `mlx_lm.lora` with the optimal configuration for this machine,
logs everything to MLflow, and saves the adapter to `adapters/<project>/`.

Requires: Python 3.11 venv with MLX-LM installed (see PYTHON_VERSION.md).

## Steps for Claude

### Step 1 — Pre-flight checks
```bash
# Check dataset exists
wc -l datalake/projects/<project>/fine-tuning-ready/train.jsonl
wc -l datalake/projects/<project>/fine-tuning-ready/valid.jsonl

# Check MLX Python is available
$MLX_PYTHON --version 2>/dev/null || echo "ERROR: set MLX_PYTHON in .env"

# Check MLflow is running
curl -s http://localhost:5001/health || echo "Start MLflow: mlflow server --port 5001"
```

Abort with clear message if any check fails.

### Step 2 — Load config
Read `config/projects/<project>.yaml` to get model name.
Read `config/models/<model>.yaml` to get training hyperparameters.
Override with any args provided (iters, etc.).

### Step 3 — Start MLflow run
```python
source .venv/bin/activate && python -m pipeline.train start-run \
    --experiment <experiment or project> \
    --project <project> \
    --model <model> \
    --iters <iters>
```
This returns a `run_id` to use for logging.

### Step 4 — Build and run training command
```bash
$MLX_PYTHON -m mlx_lm.lora \
    --model <hf_path from config> \
    --train \
    --data datalake/projects/<project>/fine-tuning-ready/ \
    --batch-size <batch_size> \
    --lora-layers <lora_layers> \
    --iters <iters> \
    --learning-rate <learning_rate> \
    --steps-per-eval <steps_per_eval> \
    --val-batches <val_batches> \
    --save-every <save_every> \
    --adapter-path adapters/<project>/<run_id>/
```

Stream output and parse `train_loss` / `val_loss` lines in real time,
pushing each step to MLflow via `python -m pipeline.train log-step`.

### Step 5 — Post-training
```bash
# Log final artifacts
python -m pipeline.train finalize --run-id <run_id> \
    --adapter-path adapters/<project>/<run_id>/

# Update config with new adapter path
python -m pipeline.deploy update-config \
    --project <project> \
    --adapter adapters/<project>/<run_id>/
```

### Step 6 — Report
Print:
- Final train_loss / val_loss
- Training duration (HH:MM:SS)
- Adapter saved to path
- MLflow run URL
- Suggest: `/eval-model project=<project> adapter=adapters/<project>/<run_id>/`

## M4 Pro optimal configs (from config/models/*.yaml)
| Model | batch_size | rank | lora_layers | RAM usage |
|-------|-----------|------|-------------|-----------|
| mistral-7b-4bit | 8 | 16 | 16 | ~18 GB |
| qwen-14b-4bit | 4 | 16 | 16 | ~32 GB |

## Expected output
```
Pre-flight: ✓ dataset (628 train / 70 valid) ✓ MLX 3.11 ✓ MLflow

MLflow run: http://localhost:5001/#/experiments/1/runs/abc123
Training: mistral-7b-4bit · 1000 iters · batch=8 · rank=16

[step 100] train_loss=2.341 val_loss=2.287
[step 200] train_loss=1.893 val_loss=1.921
...
[step 1000] train_loss=0.821 val_loss=0.934

Duration: 00:47:23
Adapter: adapters/migrator/abc123/

Next: /eval-model project=migrator adapter=adapters/migrator/abc123/
```
