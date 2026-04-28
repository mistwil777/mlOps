# CLAUDE.md — MLOps Project
# Read by CC at every session startup in this folder.

## Project Context
MLOps infrastructure for producing fine-tuned local models.
Goal: complete AI sovereignty — 100% local inference, zero cloud calls in production.
Current scope: Migrator (Python→Java migration). Future: HR, Legal, Sales, Defense.

Company: Capgemini — ~50% of clients in Defense sector (sovereignty constraints, classified data).

## Paths
- This repo: ~/Documents/GitHub/mlOps/
- Migrator source: ~/migrator/ (read-only — do not modify)
- Target migration: ~/Documents/GitHub/migrator/ (after move — see MIGRATION.md)

## Machine
MacBook Pro M4 Pro · 48 GB RAM · macOS Tahoe 26.4
Recommended batch_size: 8 (7B 4-bit models) · 4 (14B 4-bit models)

## Datalake Structure
- datalake/raw/               → IMMUTABLE after ingestion — never write here
- datalake/processed/         → after cleaning pipeline
- datalake/fine-tuning-ready/ → JSONL ready for mlx_lm.lora
- models/staging/<project>/   → fused models awaiting gate (mlx_lm.fuse output)
- models/production/<project>/→ gate-passed models only — serving allowed here
- adapters/                   → LoRA checkpoints
- projects/<name>/            → data per project/domain

## Active Tech Stack
Fine-tuning   : MLX-LM (QLoRA, LoRA) — requires Python 3.11 (see PYTHON_VERSION.md)
Vector dev    : LanceDB (serverless, ~/Documents/GitHub/mlOps/lancedb/)
Vector prod   : Qdrant (Docker, port 6333) — after Phase 2
Tracking      : MLflow local (experiments/, port 5001)
Versioning    : DVC (local remote until MinIO is available)
Metadata DB   : SQLite local (experiments/mlops.db) then PostgreSQL
Serving       : mlx_lm.server --port 8080
Embedding     : nomic-embed-text v1.5 via Ollama (port 11434)

## Active Projects
- migrator: Python→Java migration · Base model: Mistral 7B Instruct 4-bit
  Source data: ~/migrator/ (read-only)
  Dataset: datalake/projects/migrator/
  Deployed model: models/mistral-migration-java/ (if exists)
  AWS Bedrock baseline: Nova Pro (us.amazon.nova-pro-v1:0, us-east-1)

## Absolute Rules
1. Zero client/classified data in CC prompts
2. raw/ is immutable — write forbidden after initial ingestion
3. Every mlx_lm.lora run → mandatory MLflow log
4. Every model → complete eval BEFORE deploy
5. All tools/actions → visible in dashboards/
6. JSONL format: always verify special tokens before training
7. Everything automatable by software must be automated — AI only where it adds real value
8. Green IT: reuse configurations, avoid redundancy, no cloud calls in production
9. models/staging/ → never served directly — promote via scripts/deploy_gate.py gate+promote
10. Modify a skill → atomic commit tagged skill/<name>/vX.Y + update SKILLS.md

## Available Commands (skills)
/new-project      : initialize a new fine-tuning project
/extract-pairs    : extract pairs from a repo
/clean-dataset    : complete cleaning pipeline
/augment-dataset  : generate synthetic data
/collect-web      : tech watch + new pairs
/train-lora       : launch fine-tuning with optimal M4 Pro config
/eval-model       : complete evaluation + baseline comparison
/deploy-model     : fuse → staging, gate check, promote → production, serve
/compare-models   : comparative table of N models
/daily-report     : aggregated daily report
/benchmark-all    : complete benchmarks across all versions
/advisor          : scoping multi-adapter — recommande adapters + données selon besoin client
/orchestrate      : routing + appels parallèles + synthèse multi-adapter

Versioning: voir SKILLS.md — règle: commit atomique tagué skill/<name>/vX.Y

## Token Economy — CC Rules
- Repetitive tasks (pair scoring) → Agent subtype haiku
- Codebase exploration → Agent subtype Explore
- Files >300 lines → request summary before full read
- MCP sqlite/postgres for metadata → don't read .db files as binary
- /compact after long sessions (>30 exchanges)
- Prefer short + precise questions over long explanations

## Expected Response Format
- Code: concise, no obvious comments, type hints everywhere, pathlib.Path only
- No end-of-response summary · No "here's what I did"
- If ambiguous: ask the question, don't assume
- Propose alternatives when the chosen approach has known limitations
- Be critical: flag potential problems even if not asked
- Conversations and info documents: French
- Code, docstrings, inline comments, log messages: English
