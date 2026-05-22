SHELL := /bin/zsh
PYTHON := python
VENV   := .venv
API_PORT  := 8002
UI_PORT   := 5173
MLFLOW_PORT := 5001

.DEFAULT_GOAL := help

# ── Help ──────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo "MLOps — available targets"
	@echo ""
	@echo "  Setup"
	@echo "    make setup          Install all dependencies (Python venv + npm)"
	@echo "    make setup-python   Python venv only"
	@echo "    make setup-ui       Frontend npm install only"
	@echo ""
	@echo "  Services"
	@echo "    make services       Start Qdrant + MLflow"
	@echo "    make services-stop  Stop Docker services"
	@echo "    make api            Start FastAPI backend (port $(API_PORT))"
	@echo "    make ui             Start SvelteKit dev server (port $(UI_PORT))"
	@echo "    make dev            Start api + ui concurrently"
	@echo ""
	@echo "  Tests"
	@echo "    make test           Run all Python tests"
	@echo "    make test-fast      Run tests without slow integration tests"
	@echo "    make lint           Ruff + type check"
	@echo ""
	@echo "  Pipeline (Migrator)"
	@echo "    make extract        Extract pairs from ~/migrator"
	@echo "    make clean-data     Clean + split dataset"
	@echo "    make rag            Build Qdrant RAG index"
	@echo "    make train          Launch LoRA fine-tuning"
	@echo "    make eval           Evaluate latest run vs baseline"
	@echo "    make deploy         Gate check + promote to production"
	@echo ""
	@echo "  Utilities"
	@echo "    make status         Show service health"
	@echo "    make logs           Tail API logs"
	@echo "    make clean          Remove __pycache__, .pyc files"

# ── Setup ────────────────────────────────────────────────────────────────────
.PHONY: setup setup-python setup-ui
setup: setup-python setup-ui

setup-python:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements.txt

setup-ui:
	cd frontend && npm install

# ── Services (Docker) ────────────────────────────────────────────────────────
.PHONY: start services services-stop
start:
	./scripts/start_services.sh

services:
	docker compose up -d qdrant mlflow
	@echo "Qdrant: http://localhost:6333"
	@echo "MLflow: http://localhost:$(MLFLOW_PORT)"

services-stop:
	docker compose down 2>/dev/null || true

# ── Dev servers ──────────────────────────────────────────────────────────────
.PHONY: api ui dev
api:
	$(VENV)/bin/uvicorn api.main:app --reload --port $(API_PORT)

ui:
	cd frontend && npm run dev

dev:
	$(MAKE) -j2 api ui

# ── Tests ────────────────────────────────────────────────────────────────────
.PHONY: test test-fast lint
test:
	$(VENV)/bin/pytest tests/ -v

test-fast:
	$(VENV)/bin/pytest tests/ -v -m "not slow"

lint:
	$(VENV)/bin/ruff check pipeline/ api/ scripts/ agents/
	$(VENV)/bin/ruff format --check pipeline/ api/ scripts/ agents/

# ── Pipeline shortcuts ───────────────────────────────────────────────────────
PROJECT ?= migrator

.PHONY: extract clean-data rag train eval deploy
extract:
	$(VENV)/bin/python -m pipeline.extract --project $(PROJECT)

clean-raw:
	$(VENV)/bin/python -m pipeline.clean_raw --project $(PROJECT)

clean-raw-dry:
	$(VENV)/bin/python -m pipeline.clean_raw --project $(PROJECT) --dry-run

clean-data:
	$(VENV)/bin/python -m pipeline.clean --project $(PROJECT)

rag:
	$(VENV)/bin/python -m pipeline.rag_index --project $(PROJECT)

train:
	$(VENV)/bin/python -m pipeline.train --project $(PROJECT)

eval:
	$(VENV)/bin/python scripts/eval_model.py --project $(PROJECT)

deploy:
	$(VENV)/bin/python scripts/deploy_gate.py --project $(PROJECT)

# ── Utilities ────────────────────────────────────────────────────────────────
.PHONY: status logs clean
status:
	@echo "=== Docker services ===" && docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not running"
	@echo "=== Ollama ===" && ollama list 2>/dev/null || echo "Ollama not running"
	@echo "=== API ===" && curl -s http://localhost:$(API_PORT)/health | python -m json.tool 2>/dev/null || echo "API not running"

logs:
	tail -f logs/*.log 2>/dev/null || echo "No log files found"

clean:
	find . -type d -name __pycache__ -not -path "./.venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./.venv/*" -delete 2>/dev/null || true
