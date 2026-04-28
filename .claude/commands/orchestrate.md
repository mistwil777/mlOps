<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /orchestrate — Multi-adapter routing + synthesis

## Usage
```
/orchestrate project=<name> question="<question>" [max_specialists=3]
```

## What this skill does
Routes a complex question through the multi-adapter stack:
1. Calls the orchestrator adapter to select relevant specialists
2. Calls selected specialists in parallel
3. Calls the orchestrator to synthesize all responses into one answer
4. Returns the final answer with full traceability

All calls are local (mlx_lm.server instances) — zero cloud calls.

## Prerequisites
- At least one specialist adapter must be serving (`status: serving` in project config)
- Orchestrator adapter must be serving (optional but recommended)
- Adapter ports must be set in `config/projects/<name>.yaml` under `adapters[].port`

## Steps for Claude

### Step 1 — Check adapter status
```bash
curl -s http://localhost:8000/api/orchestrate/status/<project>
```
Expected: `ready: true`, at least one specialist.

If adapters are not serving: run `/deploy-model` per adapter first.

### Step 2 — Call the orchestrator
```bash
curl -s -X POST http://localhost:8000/api/orchestrate/query \
  -H "Content-Type: application/json" \
  -d '{
    "project": "<name>",
    "question": "<question>",
    "max_specialists": 3
  }'
```

### Step 3 — Display results
Print the answer with routing trace:

```
Question: <question>

Routing → [droit-societes, droit-ip] (via orchestrateur-juridique)

Specialist responses:
  droit-societes (142ms): ...
  droit-ip (158ms): ...

Synthesized answer (orchestrateur-juridique, 89ms):
  <final answer>

Total latency: 389ms
```

## Port assignment convention
When deploying multiple adapters for the same project, use sequential ports:
- Base project: 8080
- Adapter 1 (specialist): 8081
- Adapter 2 (specialist): 8082
- Orchestrator: 8090

Update `config/projects/<name>.yaml`:
```yaml
adapters:
  - name: droit-societes
    role: specialist
    status: serving
    port: 8081
  - name: orchestrateur-juridique
    role: orchestrator
    status: serving
    port: 8090
```

## Embedded deployment (no RAG, fused model)
For sovereign/classified contexts without RAG:
```bash
# Fuse all specialist adapters into one model
python -m scripts.fuse_multi_adapter \
    --project <name> \
    --adapters droit-societes,droit-ip,droit-contrats \
    --output models/production/<name>/fused-all/
```
The fused model answers all domains without multi-process overhead.

## Expected output
```
Orchestration: juridique · 2 specialists called · 1 orchestrator

Routing: [droit-societes, droit-ip]
Latencies: droit-societes=142ms · droit-ip=158ms · synthesis=89ms

Answer:
  <synthesized response with citations>

Total: 389ms
```
