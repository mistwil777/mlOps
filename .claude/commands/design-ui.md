<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /design-ui — Concevoir ou réviser l'architecture UI/UX de la plateforme

## Usage
```
/design-ui [scope=<page|full>] [inspiration=<project>]
```

## Ce que fait ce skill

Analyse l'état actuel de l'UI SvelteKit, propose une architecture UI/UX orientée **workflow guidé par étapes** adapté à une plateforme MLOps multi-projet avec contraintes d'ordre.

Produit :
1. Critique de l'UI actuelle (ce qui bloque le workflow)
2. Architecture cible (structure des routes, composants clés)
3. Maquette ASCII de la page principale
4. Plan de migration incrémental (quoi construire en premier)
5. Recommandations Green IT spécifiques à l'UI

**Ce skill utilise Claude Opus** — travail architectural, pas de génération de code.

## Principes directeurs

- **Workflow-first** : l'UI guide l'utilisateur dans l'ordre correct (extract → clean → train → eval → gate → deploy), pas un menu libre
- **Project-centric** : chaque projet est une entité avec son propre état de pipeline — pas de vues globales mélangeant tous les projets
- **Locked steps** : une étape est grisée tant que la précédente n'est pas validée (gate vert)
- **Status-at-a-glance** : on voit l'état de chaque étape sans cliquer
- **Minimal tokens** : pas de navigation libre qui force CC à re-lire tout le contexte
- **Green IT** : composants partagés, pas de duplication, SSE pour les updates (pas de polling)

## Inspirations de référence

| Projet | Ce qu'on prend |
|---|---|
| **Metaflow UI** | DAG de pipeline par run, chaque step cliquable |
| **Label Studio** | Wizard étape par étape, progression visible |
| **Weights & Biases** | Vue projet centrale, compare automatique |
| **Prefect UI** | Flow graph interactif, locked steps si précédent failed |
| **Vercel Deploy** | Timeline de déploiement simple, statut en temps réel |

## Steps pour Claude

### Step 1 — Audit de l'UI actuelle
```bash
find frontend/src/routes -name "*.svelte" | sort
```
Lire les pages clés : layout, projects/[id], train, models, playground.

Identifier :
- Les navigations libres sans contrainte d'ordre
- Les pages qui dupliquent des informations
- Les composants manquants pour le workflow guidé

### Step 2 — Analyser l'API disponible
```bash
grep -r "router.get\|router.post" api/routers/ | grep -v "__pycache__"
```
Mapper les endpoints disponibles aux étapes du pipeline.

### Step 3 — Proposer l'architecture cible

Structure de routes recommandée :
```
/                          → hub global (projets + alertes)
/projects/new              → wizard création projet (advisor intégré)
/projects/[id]             → pipeline view (timeline 7 étapes)
/projects/[id]/step/[n]    → page dédiée à une étape
/playground/[project]      → test modèle en cours
/delivery/[project]        → export + sovereign packaging
/admin/models              → base models (rare, pas en nav principale)
/admin/settings            → config globale
```

### Step 4 — Maquette ASCII de la vue projet

```
╔══════════════════════════════════════════════════════════╗
║  ◈ juridique-rh                        [standard] [live] ║
║  Qwen2.5-Coder-7B · 1 243 pairs · run #7 FINISHED       ║
╠══════════════════════════════════════════════════════════╣
║                                                          ║
║  1 EXTRACT ✓   2 CLEAN ✓   3 AUGMENT ✓   4 TRAIN ✓     ║
║  ──────────────────────────────── ▶                      ║
║  5 EVAL  ◐ running   6 GATE ○ locked   7 DEPLOY ○ locked ║
║                                                          ║
║  ┌─ Étape en cours : EVAL ──────────────────────────┐   ║
║  │  pass_rate  ████░░  0.78 / 0.85 ↑               │   ║
║  │  rouge_l    ██████  0.71 / 0.65 ✓               │   ║
║  │  latency    ██░░░░  1.4x / 2.0x ✓               │   ║
║  │  [Annuler]  [Voir logs]                          │   ║
║  └───────────────────────────────────────────────────┘   ║
╚══════════════════════════════════════════════════════════╝
```

### Step 5 — Plan de migration incrémental

Proposer dans quel ordre construire sans casser l'existant :
1. Composant `PipelineTimeline.svelte` (réutilisable partout)
2. Refactor `projects/[id]` → vue pipeline centrale
3. Pages `projects/[id]/step/[n]` (replace train/, data/, eval/)
4. Hub `/` simplifié (juste les projets + alertes)
5. Supprimer les pages orphelines (data/, models/, adapters/ → dans les steps)

### Step 6 — Recommandations Green IT UI

- SSE au lieu de polling setInterval (déjà partiellement là — vérifier)
- Lazy-load les pages de steps (pas tout charger au démarrage)
- Cache côté serveur sur les métriques d'éval (TTL 5 min)
- Éviter les composants qui fetchent la même donnée en doublon

## Output attendu

```
Architecture UI — <scope>

Critique actuelle:
  ✗ [liste des problèmes]

Architecture cible:
  [routes + composants]

Plan de migration:
  Step 1: ...
  Step 2: ...

Green IT:
  [recommandations spécifiques]
```
