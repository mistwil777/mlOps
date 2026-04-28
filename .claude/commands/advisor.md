<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /advisor — Multi-adapter scoping for a new project

## Usage
```
/advisor project=<name> need="<free text description of client need>"
```

## What this skill does
Analyses the client need in natural language and recommends:
1. The list of specialized adapters to fine-tune (micro-specialists)
2. The orchestrator adapter role and its training data nature
3. The data types and sources to collect per adapter
4. Whether RAG is relevant (and if so, for which adapters)
5. Estimated dataset sizes and complexity

Uses local Ollama (zero cloud calls). Writes recommendations to
`config/projects/<name>.yaml` under the `adapters:` key.

## Steps for Claude

### Step 1 — Check Ollama is available
```bash
curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])"
```
Use the best available local model (prefer qwen2.5 or mistral).

### Step 2 — Call POST /api/advisor/recommend
```bash
curl -s -X POST http://localhost:8000/api/advisor/recommend \
  -H "Content-Type: application/json" \
  -d '{"project": "<name>", "need": "<free text>"}'
```

The endpoint calls Ollama with a structured prompt and returns:
```json
{
  "adapters": [
    {
      "name": "droit-societes",
      "role": "specialist",
      "description": "Droit des sociétés — constitution, gouvernance, dissolution",
      "data_types": ["textes de loi", "jurisprudence", "contrats types"],
      "data_sources": ["Légifrance", "Dalloz extraits libres", "INPI"],
      "rag_recommended": true,
      "estimated_pairs": 800
    }
  ],
  "orchestrator": {
    "name": "orchestrateur-juridique",
    "role": "orchestrator",
    "description": "Route les questions, appelle les spécialistes, synthétise",
    "data_types": ["exemples de routing", "paires question→adapter(s)→réponse synthétisée"],
    "estimated_pairs": 200
  },
  "rag_global": false,
  "notes": "Pour contexte embarqué: désactiver RAG, fusionner adapters complémentaires"
}
```

### Step 3 — Display recommendations
Print a formatted table:

```
Adapters recommandés pour <project> :

  Spécialistes (N adapters) :
  ┌────────────────────────┬─────────────────────────────────────┬──────────┬────────┐
  │ Adapter                │ Domaine                             │ RAG      │ Paires │
  ├────────────────────────┼─────────────────────────────────────┼──────────┼────────┤
  │ droit-societes         │ Constitution, gouvernance, dissolution│ Oui     │ ~800   │
  │ droit-ip               │ Brevets, marques, droits d'auteur   │ Oui      │ ~600   │
  │ droit-contrats         │ Rédaction et analyse contractuelle  │ Non      │ ~500   │
  └────────────────────────┴─────────────────────────────────────┴──────────┴────────┘

  Orchestrateur :
  │ orchestrateur-juridique│ Routing + synthèse multi-adapter    │ Non      │ ~200   │

  Sources de données recommandées :
  - droit-societes : Légifrance (textes libres), INPI, jurisprudence Cour de cassation
  - droit-ip       : INPI, EUIPO, brevets libres EPO
  - droit-contrats : modèles UNIDROIT, accords types secteur public

  Note : contexte embarqué → désactiver RAG, fusionner adapters au déploiement
```

### Step 4 — Update project config
Write the adapter list to `config/projects/<name>.yaml`:
```yaml
adapters:
  - name: droit-societes
    role: specialist
    status: pending        # pending | training | ready
    rag: true
    estimated_pairs: 800
  - name: orchestrateur-juridique
    role: orchestrator
    status: pending
    rag: false
    estimated_pairs: 200
```

Also set `multi_adapter: true` and `isolation_level: sovereign` if classification
is `confidentiel` or `secret`.

### Step 5 — Suggest next steps
For each adapter: `/new-project name=<project>-<adapter>` to initialize sub-projects.
Then: `/extract-pairs` or `/collect-web` per adapter.

## Expected output
```
Analyse du besoin : "système d'aide juridique pour cabinets d'avocats défense"

5 adapters recommandés + 1 orchestrateur
Complexité estimée : haute · Paires totales : ~2900

config/projects/juridique.yaml mis à jour
Prochaine étape : /new-project name=juridique-droit-societes classification=confidentiel
```
