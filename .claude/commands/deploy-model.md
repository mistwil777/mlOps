# /deploy-model — Fuse adapter + serve + sanity check + rollback capability

## Usage
```
/deploy-model project=<name> adapter=<adapter_path> [port=8080] [dry-run=true]
```

## What this skill does
1. Fuses the LoRA adapter into the base model
2. Runs a sanity check (10 test requests)
3. Starts the model server
4. Updates the project config to point to the new model
5. Preserves the previous model path for rollback

Requires: Python 3.11 venv for MLX operations.

## Steps for Claude

### Step 1 — Pre-deploy gate
Check eval results exist for this adapter:
```bash
ls experiments/eval_<project>_*.json | xargs ls -t | head -1
```
If no eval found: abort with message "Run /eval-model first".
If last eval has failed gates: abort unless `--force` flag is set.

### Step 2 — Fuse adapter into base model
```bash
$MLX_PYTHON -m mlx_lm.fuse \
    --model <hf_path from project config> \
    --adapter-path <adapter_path> \
    --save-path models/<project>/<run_id>/
```

### Step 3 — Sanity check (10 requests)
```bash
python -m pipeline.deploy sanity-check \
    --model models/<project>/<run_id>/ \
    --project <project> \
    --n 10
```

Checks:
- Model loads without error
- 10 known prompts return valid, compilable Java
- Response time < 10s per request
- No crashes or OOM errors

### Step 4 — Deploy (if not dry-run)
```bash
python -m pipeline.deploy serve \
    --model models/<project>/<run_id>/ \
    --port <port>
```

Stops any existing server on that port first (graceful shutdown).
Starts server as background process, writes PID to `models/<project>/server.pid`.

### Step 5 — Update config and save rollback pointer
```bash
python -m pipeline.deploy update-config \
    --project <project> \
    --new-model models/<project>/<run_id>/ \
    --save-rollback
```

This writes to `config/projects/<project>.yaml`:
- `model_path: models/<project>/<run_id>/`
- `previous_model_path: <old path>`
- `deployed_at: <timestamp>`

### Step 6 — Smoke test
```bash
curl -s http://localhost:<port>/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"local","messages":[{"role":"user","content":"Convert this Python to Java: x = 1"}],"max_tokens":100}'
```

### Rollback procedure (document only — do not execute)
```bash
python -m pipeline.deploy rollback --project <project>
# Reads previous_model_path from config, restarts server on that model
```

## Expected output
```
Pre-deploy gate: ✓ eval found (2026-04-23) · ✓ all gates passed

Fusing adapter → models/migrator/abc123/ ... done (4m 12s)

Sanity check (10 requests):
  10/10 valid Java · 10/10 compilable · avg latency: 138ms ✓

Server started: http://localhost:8080/v1
PID: 12345 → models/migrator/server.pid

Config updated: config/projects/migrator.yaml
Rollback saved: models/migrator/previous_model_path

Smoke test: ✓ HTTP 200 · valid response

Model deployed: models/migrator/abc123/
```
