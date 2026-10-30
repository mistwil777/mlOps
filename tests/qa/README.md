# tests/qa — Cahiers des charges de test manuel

## Structure

```
tests/qa/
  README.md                          ← ce fichier
  qa_store.py                        ← SQLite store (résultats de test)
  cdc-test-advisor-phase0.html       ← CDC interactif Advisor Phase 0
  cdc-projet-01-tests-unitaires.html ← spec tests unitaires Projet 01
```

Les résultats sont stockés dans `experiments/qa/qa_store.db`.

## Utilisation

1. Ouvrir le CDC HTML dans un navigateur
2. Lancer la stack (`docker-compose up -d` + `uvicorn api.main:app --port 8002`)
3. Suivre les scénarios, cocher les cases
4. Remplir le champ "Testeur" (optionnel) + notes
5. Cliquer **"Enregistrer les résultats"** → POST vers `/api/qa/run`

## Endpoints API

```
POST /api/qa/run              → enregistrer une session de test
GET  /api/qa/runs             → historique des sessions
GET  /api/qa/runs/{id}        → détail d'une session (checks individuels)
GET  /api/qa/analysis         → checks classés par taux d'échec (diagnostic)
GET  /api/qa/trend/{cdc_id}   → taux de succès dans le temps (régression)
```

## Exemples curl

```bash
# Voir les derniers runs
curl http://localhost:8002/api/qa/runs | python3 -m json.tool

# Analyse des échecs (quel check échoue le plus ?)
curl "http://localhost:8002/api/qa/analysis?cdc_id=advisor-phase0" | python3 -m json.tool

# Tendance dans le temps
curl http://localhost:8002/api/qa/trend/advisor-phase0 | python3 -m json.tool
```

## CDC IDs

| Fichier | cdc_id |
|---|---|
| cdc-test-advisor-phase0.html | `advisor-phase0` |
| cdc-projet-01-tests-unitaires.html | `projet-01-tests` (spec uniquement, pas encore interactif) |
