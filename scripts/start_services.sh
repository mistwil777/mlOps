#!/usr/bin/env zsh
# Start all MLOps dev services: Docker Desktop → Qdrant + MLflow → Ollama → API
# Usage: ./scripts/start_services.sh [--skip-ollama] [--skip-api]

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO_DIR/.venv"
API_PORT=8002
MLFLOW_PORT=5001
OLLAMA_PORT=11434
DOCKER_SOCK="$HOME/.docker/run/docker.sock"
SKIP_OLLAMA=false
SKIP_API=false

for arg in "$@"; do
  [[ "$arg" == "--skip-ollama" ]] && SKIP_OLLAMA=true
  [[ "$arg" == "--skip-api" ]] && SKIP_API=true
done

log()  { echo "[$(date +%H:%M:%S)] $*"; }
ok()   { echo "[$(date +%H:%M:%S)] ✓ $*"; }
warn() { echo "[$(date +%H:%M:%S)] ⚠ $*" >&2; }
fail() { echo "[$(date +%H:%M:%S)] ✗ $*" >&2; exit 1; }

# ── 1. Docker Desktop ────────────────────────────────────────────────────────
log "Checking Docker Desktop…"
if [[ -S "$DOCKER_SOCK" ]]; then
  ok "Docker daemon already running"
else
  log "Starting Docker Desktop…"
  open -a Docker
  log "Waiting for Docker daemon (up to 60s)…"
  for i in $(seq 1 60); do
    [[ -S "$DOCKER_SOCK" ]] && break
    sleep 1
  done
  [[ -S "$DOCKER_SOCK" ]] || fail "Docker daemon did not start in 60s — check Docker Desktop manually"
  ok "Docker daemon ready"
fi

# ── 2. Qdrant + MLflow ───────────────────────────────────────────────────────
log "Starting Qdrant + MLflow…"
cd "$REPO_DIR"
docker compose up -d qdrant mlflow

log "Waiting for Qdrant (port 6333)…"
for i in $(seq 1 30); do
  curl -sf http://localhost:6333/healthz >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://localhost:6333/healthz >/dev/null 2>&1 && ok "Qdrant ready" || warn "Qdrant may not be ready yet — check: docker logs mlops-qdrant-1"

log "Waiting for MLflow (port $MLFLOW_PORT)…"
for i in $(seq 1 30); do
  curl -sf http://localhost:$MLFLOW_PORT/health >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://localhost:$MLFLOW_PORT/health >/dev/null 2>&1 && ok "MLflow ready" || warn "MLflow may not be ready yet — check: docker logs mlops-mlflow-1"

# ── 3. Ollama ────────────────────────────────────────────────────────────────
if [[ "$SKIP_OLLAMA" == "false" ]]; then
  log "Checking Ollama…"
  if curl -sf http://localhost:$OLLAMA_PORT/api/tags >/dev/null 2>&1; then
    ok "Ollama already running"
  else
    log "Starting Ollama server…"
    nohup /opt/homebrew/bin/ollama serve >"$REPO_DIR/logs/ollama.log" 2>&1 &
    for i in $(seq 1 20); do
      curl -sf http://localhost:$OLLAMA_PORT/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
    curl -sf http://localhost:$OLLAMA_PORT/api/tags >/dev/null 2>&1 && ok "Ollama server ready" || warn "Ollama server did not start — check logs/ollama.log"
  fi

  # Pull embedding model if missing (ZScaler may block — non-fatal)
  LOADED=$(curl -sf http://localhost:$OLLAMA_PORT/api/tags 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(' '.join(m['name'] for m in d.get('models',[])))") || LOADED=""
  if [[ "$LOADED" != *"nomic-embed-text"* ]]; then
    log "Pulling nomic-embed-text (may fail on ZScaler network)…"
    /opt/homebrew/bin/ollama pull nomic-embed-text:latest 2>/dev/null && ok "nomic-embed-text pulled" || warn "Could not pull nomic-embed-text (ZScaler?). Embeddings will fall back to Bedrock Titan."
  else
    ok "nomic-embed-text already loaded"
  fi

  if [[ "$LOADED" != *"qwen2.5-coder"* ]]; then
    log "qwen2.5-coder:7b not present — skipping (pull manually when off VPN: ollama pull qwen2.5-coder:7b)"
  else
    ok "qwen2.5-coder:7b present"
  fi
fi

# ── 4. FastAPI backend ───────────────────────────────────────────────────────
if [[ "$SKIP_API" == "false" ]]; then
  if curl -sf http://localhost:$API_PORT/health >/dev/null 2>&1; then
    ok "API already running on port $API_PORT"
  else
    log "Starting FastAPI backend…"
    mkdir -p "$REPO_DIR/logs"
    nohup "$VENV/bin/uvicorn" api.main:app --reload --port $API_PORT \
      >"$REPO_DIR/logs/api.log" 2>&1 &
    for i in $(seq 1 20); do
      curl -sf http://localhost:$API_PORT/health >/dev/null 2>&1 && break
      sleep 1
    done
    curl -sf http://localhost:$API_PORT/health >/dev/null 2>&1 && ok "API ready on port $API_PORT" || warn "API not yet ready — check logs/api.log"
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "── Services status ────────────────────────────────────────────────"
docker ps --format "  Docker: {{.Names}} ({{.Status}})" 2>/dev/null | grep -E "qdrant|mlflow" || true
curl -sf http://localhost:$OLLAMA_PORT/api/tags >/dev/null 2>&1 \
  && echo "  Ollama:  http://localhost:$OLLAMA_PORT  ✓" \
  || echo "  Ollama:  not running (use --skip-ollama to ignore)"
curl -sf http://localhost:$API_PORT/health >/dev/null 2>&1 \
  && echo "  API:     http://localhost:$API_PORT  ✓" \
  || echo "  API:     not running  (check logs/api.log)"
echo "  Qdrant:  http://localhost:6333"
echo "  MLflow:  http://localhost:$MLFLOW_PORT"
echo "────────────────────────────────────────────────────────────────────"
