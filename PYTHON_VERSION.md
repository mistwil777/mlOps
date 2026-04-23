# Python Version Decision

## Current setup
- **Python 3.10.13** (system, via pyenv) — used for all pipeline and agent code
- **Python 3.11** — required only for MLX-LM fine-tuning

## Why the split

MLX-LM `>=0.21` requires Python 3.11+ due to `match` statement usage in core modules
and type hint syntax (`X | Y`) that is not backported to 3.10.

All other dependencies in `requirements.txt` are compatible with Python 3.10.
Keeping the main environment on 3.10 maximizes compatibility with the broader
AI tooling ecosystem (LangChain, LanceDB, Prefect, etc.).

## Setup for fine-tuning (MLX-LM)

```bash
# Install Python 3.11 via pyenv
pyenv install 3.11.11
pyenv virtualenv 3.11.11 mlops-mlx

# Activate and install MLX-LM only
pyenv activate mlops-mlx
pip install mlx-lm

# Return to default env for all other work
pyenv deactivate
```

The `train.py` module invokes `mlx_lm.lora` via subprocess (`python -m mlx_lm.lora ...`)
pointing to the `mlops-mlx` interpreter, so the rest of the pipeline runs on 3.10.

## If 3.11 is not available

The `pipeline/train.py` module detects the MLX interpreter at runtime:

```python
MLX_PYTHON = os.environ.get("MLX_PYTHON", "python3.11")
```

Set `MLX_PYTHON` in your `.env` to point to the correct interpreter path.

## Decision log
- 2026-04-23: Chose 3.10 for main env, 3.11 for MLX-LM venv
- Reason: MLX is considered provisional in the stack (may be replaced by
  a different training framework); locking the whole project to 3.11 for
  a single tool would be premature
