# Stack technique — MLOps Factory

> **À qui s'adresse ce document** : développeurs, data scientists, architectes, responsables de projet, clients. Aucun prérequis technique n'est nécessaire pour comprendre le rôle de chaque outil.

---

## Vue d'ensemble

La plateforme MLOps Factory produit des modèles d'IA **entièrement locaux** — aucune donnée ne sort du réseau interne, aucun appel cloud en production. Elle couvre toute la chaîne : collecte des données → nettoyage → fine-tuning → évaluation → livraison chez le client.

Elle se divise en deux environnements distincts :

| Environnement | Objectif | Qui l'utilise |
|---|---|---|
| **Plateforme interne Capgemini** | Produire et évaluer les modèles | Ingénieurs MLOps Capgemini |
| **Bundle client** | Faire tourner le modèle livré | Équipes techniques du client |

---

## Outils — Plateforme interne

### 1. SvelteKit — Interface web (frontend)

**Rôle** : Interface utilisateur de la plateforme. Accessible depuis un navigateur, elle permet de gérer les projets, lancer des analyses, suivre l'entraînement, consulter les évaluations et déclencher les livraisons.

**Technologies** : SvelteKit (framework JavaScript), TypeScript, CSS variables.

**Liens** : Consomme exclusivement l'API FastAPI via des appels HTTP (`/api/…`). Aucun accès direct aux bases de données.

---

### 2. FastAPI — API backend

**Rôle** : Cerveau de la plateforme. Expose tous les points d'accès HTTP (`/api/`) appelés par l'interface web. Orchestre les appels aux autres services (base de données, Qdrant, MLflow, pipelines Python).

**Technologies** : Python, FastAPI, Pydantic (validation des données), Uvicorn (serveur HTTP).

**Liens** :
- Reçoit les requêtes du frontend SvelteKit
- Lit/écrit dans SQLite (métadonnées projets et runs)
- Lance les scripts de pipeline (entraînement, nettoyage, indexation)
- Interroge Qdrant pour la recherche sémantique
- Enregistre les métriques dans MLflow
- Appelle Claude (Anthropic API) pour l'Advisor et l'analyse de paires

---

### 3. SQLite — Base de données métadonnées

**Rôle** : Stockage des métadonnées de chaque projet : liste des projets, configurations, statuts des runs d'entraînement, résultats d'évaluations. Une base par projet (`experiments/projects/<id>/mlops.db`).

**Technologies** : SQLite (base de données embarquée, un seul fichier).

**Pourquoi SQLite et pas PostgreSQL** : Usage mono-utilisateur en phase 1. PostgreSQL est prévu pour la phase 2 (accès concurrent multi-équipes).

**Liens** : Accédée uniquement par l'API FastAPI. Jamais exposée directement au frontend.

---

### 4. MLflow — Suivi des expériences d'entraînement

**Rôle** : Enregistre automatiquement chaque run d'entraînement : hyperparamètres utilisés (learning rate, batch size…), métriques obtenues (loss, BLEU score…), durée, modèle produit. Permet de comparer les expériences et de retrouver les conditions exactes d'un run passé.

**Technologies** : MLflow (open source, Meta/community), interface web sur port 5001, stockage local.

**Liens** :
- Alimenté par l'API FastAPI après chaque run
- Consulté via son interface web propre (pas dans la plateforme MLOps)
- **Absent du bundle client** : usage interne Capgemini uniquement

---

### 5. MLX-LM — Fine-tuning des modèles

**Rôle** : Effectue l'entraînement (fine-tuning) du modèle de base sur les données du projet. Utilise la technique **LoRA/QLoRA** (Low-Rank Adaptation) : au lieu de ré-entraîner l'intégralité du modèle (des milliards de paramètres), on entraîne seulement de petites matrices d'adaptation — les **adaptateurs**. C'est 10× plus rapide et 10× plus léger.

**Technologies** : MLX-LM (framework Apple pour M-series), Python 3.11, accélération GPU via Metal (puce M4 Pro).

**Pourquoi MLX** : Optimisé pour les puces Apple Silicon (M1/M2/M3/M4). Performances équivalentes à une GPU Nvidia A100 pour les modèles ≤14B paramètres, sur le MacBook de développement.

**Liens** :
- Lance depuis l'API FastAPI (sous-processus)
- Lit les données JSONL du datalake (`fine-tuning-ready/`)
- Produit des adaptateurs LoRA (fichiers `.safetensors`) dans `adapters/`
- Log les métriques vers MLflow

---

### 6. Qdrant — Base vectorielle

**Rôle** : Stocke des représentations mathématiques (vecteurs) de textes pour permettre la **recherche par sens** plutôt que par mots-clés. Deux usages distincts :

1. **RAG (Retrieval-Augmented Generation)** : lors d'une inférence, le routeur cherche dans Qdrant les passages de documentation les plus pertinents pour enrichir le contexte du modèle.
2. **Datalake search** : la plateforme interne peut chercher par sens dans toutes les paires d'entraînement de tous les projets (ex : "réglementation légale employeur" trouve les paires liées même sans les mots exacts).
3. **Détection de réutilisation** : lors du wizard de création de projet, détecte si des données existantes d'un autre projet couvrent déjà un besoin.

**Technologies** : Qdrant (open source, Rust), Docker, port 6333. Collections : `patterns_{project_id}` (RAG), `datalake_pairs` (recherche globale).

**Liens** :
- Alimenté par `pipeline/rag_index.py` (vecteurs RAG) et `pipeline/datalake_index.py` (paires datalake)
- Interrogé par l'API FastAPI pour la recherche
- Interrogé par le **routeur client** en production pour le RAG
- Présent dans le bundle client (snapshot des vecteurs RAG du projet)

---

### 7. Sentence Transformers — Modèle d'embedding

**Rôle** : Transforme un texte en vecteur numérique (768 nombres) qui capture le sens de ce texte. Deux textes sémantiquement proches auront des vecteurs proches. C'est ce vecteur qui est stocké dans Qdrant.

**Modèle utilisé** : `nomic-embed-text-v1.5` (open source, 768 dimensions, distance cosinus).

**Technologies** : `sentence-transformers` (Python), exécuté localement — aucun appel externe.

**Liens** : Utilisé par `pipeline/rag_index.py` et `pipeline/datalake_index.py` avant chaque écriture dans Qdrant.

---

### 8. Claude (Anthropic API) — IA d'assistance au design

**Rôle** : Utilisé **uniquement en phase de conception** (côté Capgemini, jamais chez le client). Trois fonctions :

1. **Advisor** : à partir d'une description de besoin métier, recommande quels adaptateurs créer, quelles données collecter, comment structurer le projet.
2. **Analyse de paires** : évalue la qualité des paires d'entraînement collectées (score, problèmes détectés, génération de paires synthétiques supplémentaires).
3. **Cross-project** : détecte si des données existantes d'autres projets couvrent déjà un besoin.

**Modèles utilisés** :
- `claude-sonnet-4-6` : tâches complexes (recommandations Advisor, analyse approfondie)
- `claude-haiku-4-5` : tâches volumineuses (scoring de nombreuses paires, génération en masse) — choix Green IT

**Liens** : Appelé par l'API FastAPI (routeurs `advisor.py`). Données : uniquement des descriptions textuelles anonymisées — jamais de données client brutes.

---

### 9. AWS Bedrock (Nova Pro) — Baseline de comparaison uniquement

**Rôle** : Modèle de référence cloud utilisé **uniquement pour les benchmarks** internes Capgemini. Permet de mesurer si le modèle fine-tuné local est aussi bon (ou meilleur) que la solution cloud avant livraison.

**Technologies** : `boto3` (SDK AWS), région `us-east-1`, modèle `us.amazon.nova-pro-v1:0`.

**Important** : N'est jamais dans le bundle client. Jamais utilisé avec des données classifiées.

**Liens** : Appelé depuis `pipeline/evaluate.py` uniquement lors des évaluations comparatives.

---

### 10. DVC — Versioning des données

**Rôle** : Versionne les fichiers de données (JSONL, datasets) de la même façon que Git versionne le code. Permet de savoir exactement quelles données ont produit quel modèle, et de revenir à une version antérieure.

**Technologies** : DVC (open source), remote local (MinIO S3-compatible prévu en phase 2).

**Liens** : Opère sur le dossier `datalake/`. Indépendant de l'API — outil ligne de commande utilisé manuellement par les ingénieurs.

---

### 11. Crawl4AI — Collecte web automatisée

**Rôle** : Scrape des sites web techniques pour collecter automatiquement des paires question/réponse dans le domaine cible. Comprend le JavaScript, contourne les protections basiques, extrait le contenu structuré.

**Technologies** : `crawl4ai` (Python), asyncio.

**Liens** : Lancé depuis l'API FastAPI (`data.py`), dépose les paires collectées dans `datalake/projects/<id>/raw/`.

---

### 12. Tree-sitter — Analyse syntaxique de code

**Rôle** : Analyse la structure du code source (Python, Java) pour extraire des paires d'entraînement pertinentes : fonction Python → équivalent Java, avec contexte syntaxique exact (pas seulement du texte brut).

**Technologies** : `tree-sitter` (parser universel, Rust), bindings Python, grammaires Python et Java.

**Liens** : Utilisé par `pipeline/extract.py` lors de l'ingestion de dépôts de code.

---

### 13. Polars — Traitement des données

**Rôle** : Manipulation et nettoyage des datasets à grande échelle. Calcule les statistiques de qualité, déduplique, filtre, transforme les formats.

**Technologies** : Polars (Python, Rust sous le capot) — plus rapide que Pandas pour les gros volumes.

**Liens** : Utilisé dans `pipeline/clean.py` et `pipeline/evaluate.py`.

---

### 14. Prefect — Orchestration des pipelines

**Rôle** : Planifie et orchestre l'exécution des pipelines de données (nettoyage, enrichissement, indexation). Permet de définir des flows avec retry automatique, dépendances entre tâches, et monitoring.

**Technologies** : Prefect 3 (open source), serveur local.

**Liens** : Enveloppe les scripts `pipeline/`. Pas encore activé en production — prévu phase 2.

---

### 15. pdfplumber / python-docx — Extraction documentaire

**Rôle** : Lit les PDF et fichiers Word uploadés par l'utilisateur dans l'Advisor pour en extraire le texte et l'utiliser comme contexte de recommandation.

**Technologies** : `pdfplumber` (PDF), `python-docx` (Word).

**Liens** : Utilisé par l'endpoint `/api/advisor/extract-doc`.

---

## Outils — Bundle client (livraison)

> Ce bundle est livré au client. Il tourne sur son infrastructure, sans accès internet.

### 16. Ollama — Serveur d'inférence

**Rôle** : Charge le modèle de base et les adaptateurs LoRA, expose une API HTTP (`/api/generate`, `/api/chat`) pour interroger le modèle. Compatible avec la plupart des clients LLM du marché.

**Technologies** : Ollama (open source), Docker, port 11434.

**Pourquoi Ollama et pas autre chose** : Charge dynamiquement plusieurs adaptateurs LoRA sans recharger le modèle de base — idéal pour les bundles multi-adaptateurs.

**Liens** :
- Charge les modèles depuis `MODELS_PATH` (lecture seule)
- Charge les adaptateurs depuis `ADAPTERS_PATH` (lecture seule)
- Reçoit les requêtes du **routeur**

---

### 17. Routeur HTTP — Orchestrateur d'adaptateurs

**Rôle** : Point d'entrée unique pour le client. Analyse chaque requête entrante, détermine quel(s) adaptateur(s) appeler, interroge Qdrant pour récupérer le contexte RAG pertinent, envoie le prompt enrichi à Ollama, renvoie la réponse synthétisée.

**Technologies** : Image Docker `capgemini/mlops-router`, Python FastAPI.

**Liens** :
- Reçoit les requêtes de l'application cliente
- Interroge **Qdrant** pour le contexte RAG
- Envoie les prompts à **Ollama**
- Lit le registre d'adaptateurs (`/adapters/registry.json`)

---

### 18. Qdrant (bundle client) — RAG en production

**Rôle** : Même technologie que côté plateforme, mais pré-chargé uniquement avec le snapshot RAG du projet livré. Permet au routeur de retrouver les passages de documentation les plus pertinents pour chaque requête.

**Technologies** : Qdrant Docker, snapshot pré-généré par `scripts/export_bundle.py`.

**Liens** : Interrogé par le routeur HTTP uniquement.

---

## Schéma des liens

```
╔══════════════════════════════════════════════════════════════════╗
║                    PLATEFORME INTERNE CAPGEMINI                  ║
║                                                                  ║
║  ┌──────────────┐        ┌──────────────────┐                   ║
║  │  SvelteKit   │──HTTP──▶   FastAPI (API)   │                   ║
║  │  (frontend)  │◀──JSON─│                  │                   ║
║  └──────────────┘        └──────┬───────────┘                   ║
║                                 │                               ║
║          ┌──────────────────────┼─────────────────────────┐     ║
║          │                      │                         │     ║
║          ▼                      ▼                         ▼     ║
║    ┌───────────┐         ┌──────────┐              ┌──────────┐ ║
║    │  SQLite   │         │  Qdrant  │              │  MLflow  │ ║
║    │(métadata) │         │(vecteurs)│              │(tracking)│ ║
║    └───────────┘         └────┬─────┘              └──────────┘ ║
║                               │                                 ║
║                    ┌──────────┘                                  ║
║                    ▼                                             ║
║             ┌────────────┐    ┌──────────────────┐              ║
║             │  Sentence  │    │    Claude API     │              ║
║             │Transformers│    │  (Advisor/Haiku)  │              ║
║             └────────────┘    └──────────────────┘              ║
║                                                                  ║
║  ┌──────────────────────────────────────────────────────────┐   ║
║  │  Pipeline Python                                          │   ║
║  │  Crawl4AI → Tree-sitter → Polars → MLX-LM → DVC         │   ║
║  │  (collecte)  (extraction) (nettoy.) (entraîn.) (version.)│   ║
║  └──────────────────────────────────────────────────────────┘   ║
╚══════════════════════════════════════════════════════════════════╝

                              │ export_bundle.py
                              ▼

╔══════════════════════════════════════════════════════════════════╗
║                      BUNDLE CLIENT (Docker)                      ║
║                                                                  ║
║  Application cliente ──▶ Routeur HTTP ──▶ Ollama (modèle+LoRA) ║
║                               │                                  ║
║                               └──▶ Qdrant (RAG snapshot)        ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## Tableau récapitulatif

| Outil | Catégorie | Environnement | Open source |
|---|---|---|---|
| SvelteKit | Frontend web | Interne | Oui |
| FastAPI | API backend | Interne | Oui |
| SQLite | Base de données | Interne | Oui |
| MLflow | Suivi expériences | Interne | Oui |
| MLX-LM | Fine-tuning | Interne | Oui |
| Qdrant | Base vectorielle | Interne + Client | Oui |
| Sentence Transformers | Embeddings | Interne | Oui |
| Claude (Anthropic) | IA assistant design | Interne | Non |
| AWS Bedrock | Benchmark baseline | Interne (eval seul) | Non |
| DVC | Versioning données | Interne | Oui |
| Crawl4AI | Collecte web | Interne | Oui |
| Tree-sitter | Parsing code | Interne | Oui |
| Polars | Traitement données | Interne | Oui |
| Prefect | Orchestration | Interne (phase 2) | Oui |
| pdfplumber / python-docx | Extraction docs | Interne | Oui |
| Ollama | Serveur inférence | Client | Oui |
| Routeur HTTP | Orchestration | Client | Capgemini IP |

---

*Document généré le 2026-05-13 — Capgemini AI Sovereignty Platform*
