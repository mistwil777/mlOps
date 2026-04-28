<!-- version: v1.0 | date: 2026-04-28 | breaking: none -->
# /build-ui — Implémenter un composant ou une page UI selon l'architecture cible

## Usage
```
/build-ui target=<composant|page|route> [project=<name>]
```

Exemples :
```
/build-ui target=PipelineTimeline
/build-ui target=projects/[id]
/build-ui target=step/eval
/build-ui target=hub
```

## Ce que fait ce skill

Implémente un élément de l'UI SvelteKit en suivant l'architecture définie par `/design-ui`.  
Toujours partir de l'existant avant de créer. Toujours relier aux endpoints API réels.

**Ce skill utilise Claude Sonnet** — génération de code ciblée.

## Principes de code

- TypeScript strict partout (`lang="ts"`)
- Svelte 5 runes (`$state`, `$derived`, `$props`) — pas les stores Svelte 4 sauf `$page`
- Pas de lib CSS externe — utiliser les variables CSS de `app.css` (`var(--green)`, `var(--border)`, etc.)
- SSE via `$lib/sse.ts` pour les updates en temps réel — jamais `setInterval` sauf health check
- Fetch via `$lib/api.ts` — ne pas faire de fetch bruts dans les composants
- Locked steps : si `step.status === 'locked'`, la carte est `opacity: 0.4; pointer-events: none`

## Convention de statuts de step

| Statut | Couleur | Comportement |
|---|---|---|
| `done` | `var(--green)` | cliquable, résumé visible |
| `running` | `var(--blue)` | progress bar SSE |
| `failed` | `var(--red)` | cliquable, message d'erreur |
| `locked` | `var(--text-muted)` | non-cliquable, tooltip "étape précédente requise" |
| `pending` | `var(--border)` | cliquable, formulaire de lancement |

## Steps pour Claude

### Step 1 — Lire l'état actuel
```bash
find frontend/src -name "*.svelte" -o -name "*.ts" | sort
```
Lire les fichiers concernés par le target. Ne pas réécrire ce qui fonctionne.

### Step 2 — Identifier les endpoints nécessaires
```bash
grep -r "router\." api/routers/ | grep -v __pycache__ | grep "def \|async def "
```
Vérifier que les endpoints existent. Si manquants → créer dans `api/routers/` d'abord.

### Step 3 — Vérifier les types existants
```bash
cat frontend/src/lib/types.ts
cat frontend/src/lib/api.ts
```
Étendre `types.ts` et `api.ts` si nécessaire. Pas de types inline dans les composants.

### Step 4 — Implémenter

**Pour un composant** (`src/lib/components/<Name>.svelte`) :
- Props typées via `$props()`
- Pas de fetch interne sauf si composant auto-suffisant documenté comme tel
- Export named pour les types associés

**Pour une page** (`src/routes/<path>/+page.svelte`) :
- `onMount` pour le fetch initial
- SSE si la page affiche un état temps-réel (run en cours, éval running)
- Topbar avec titre + actions contextuelles

**Pour un layout** :
- Modifier `+layout.svelte` uniquement si changement de navigation globale
- Préférer un layout local `+layout.svelte` dans le sous-dossier

### Step 5 — Vérifier la cohérence Green IT
- La page ne polling pas en dehors de health check
- Pas de fetch dupliqué (même donnée chargée 2x)
- Les locked steps ne fetchent pas leurs données (inutile)

### Step 6 — Test build
```bash
npm run build --prefix frontend 2>&1 | tail -20
```
Le build doit passer sans erreur TypeScript.

## Targets standards

| Target | Description | Pages/composants concernés |
|---|---|---|
| `PipelineTimeline` | Timeline 7 étapes avec statuts | composant réutilisable |
| `StepCard` | Carte d'une étape (statut + action) | composant réutilisable |
| `projects/[id]` | Vue projet centrée pipeline | remplace tabs actuels |
| `step/extract` | Page extraction de pairs | remplace data/ingest |
| `step/clean` | Page cleaning + stats | remplace data/pipeline |
| `step/train` | Page fine-tuning | refactor train/ |
| `step/eval` | Page éval + métriques domaine | nouveau |
| `step/gate` | Page gate check + résultats | nouveau |
| `step/deploy` | Page promotion + serving | remplace delivery/ |
| `hub` | Page d'accueil simplifiée | refactor +page.svelte |
| `advisor-wizard` | Wizard création projet multi-adapter | remplace projects/new |
| `playground/[project]` | Playground lié à un projet | refactor playground/ |

## Output attendu

Fichiers créés/modifiés listés, build status, et ce qui reste à faire pour ce target.
