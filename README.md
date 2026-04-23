# MLOps — Infrastructure de souveraineté IA locale

Infrastructure MLOps partagée pour la production de modèles LLM fine-tunés 100% locaux.
Développée chez Capgemini pour des contextes à haute exigence de souveraineté (secteur Défense).

**Principe :** Claude Code et les agents construisent les outils. Une fois déployés, les modèles tournent entièrement en local — zéro appel cloud, zéro donnée client externe.

---

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           PIPELINE MLOPS                │
                    │                                         │
  Source repo  ───► │  extract  ──► clean  ──► augment        │
  (lecture seule)   │                  │                      │
                    │             datalake/                   │
                    │          fine-tuning-ready/             │
                    │                  │                      │
                    │              train (MLX-LM)             │
                    │                  │                      │
                    │             adapters/  ──► MLflow       │
                    │                  │                      │
                    │            eval (gates)                 │
                    │                  │                      │
                    │         fuse ──► models/ ──► serve      │
                    │                                         │
                    │  Agents: web_collector · quality_scorer │
                    │          report_generator               │
                    └─────────────────────────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │  LLM Server local  │ :8080 (API OpenAI-compatible)
                    │  mlx_lm.server     │
                    └────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │  Applications      │
                    │  (Migrator, RH...) │
                    └────────────────────┘
```

---

## Prérequis

- **macOS** avec puce Apple Silicon (M1/M2/M3/M4)
- **Python 3.10+** (pyenv recommandé)
- **Python 3.11** pour MLX-LM uniquement (voir [PYTHON_VERSION.md](PYTHON_VERSION.md))
- **Ollama** installé et en cours d'exécution (`ollama serve`)
- **Java JDK** pour la compilation Java dans les métriques (`javac`)
- **Git** et **DVC**
- Au moins **32 Go RAM** recommandés pour les modèles 7B 4-bit

---

## Installation en 5 étapes

```bash
# 1. Cloner le repo
git clone https://github.com/mistwil777/mlOps.git ~/Documents/GitHub/mlOps
cd ~/Documents/GitHub/mlOps

# 2. Créer l'environnement virtuel Python 3.10
python3 -m venv .venv
source .venv/bin/activate

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Configurer l'environnement
cp .env.example .env
# Éditer .env avec vos valeurs (AWS credentials pour la baseline Nova Pro, etc.)

# 5. Vérifier l'installation
python -m pytest tests/ -v
```

Pour MLX-LM (fine-tuning uniquement) :
```bash
pyenv install 3.11.11
pyenv virtualenv 3.11.11 mlops-mlx
pyenv activate mlops-mlx
pip install mlx-lm
# Ajouter dans .env : MLX_PYTHON=/Users/<user>/.pyenv/versions/mlops-mlx/bin/python
```

---

## Démarrage rapide — premier fine-tuning de bout en bout

```bash
# Démarrer MLflow
mlflow server --port 5001 &

# Démarrer Ollama (pour scoring et embeddings)
ollama serve &
ollama pull mistral

# Dans Claude Code, depuis ce dossier :
/new-project name=migrator src_lang=python tgt_lang=java model=mistral-7b classification=interne
/extract-pairs project=migrator repo=~/migrator
/clean-dataset project=migrator score=true threshold=0.7
/train-lora project=migrator iters=1000
/eval-model project=migrator adapter=adapters/migrator/<run_id>/ baseline=nova-pro
/deploy-model project=migrator adapter=adapters/migrator/<run_id>/
```

---

## Skills Claude Code disponibles

| Commande | Description |
|---|---|
| `/new-project` | Initialiser un nouveau projet de fine-tuning |
| `/extract-pairs` | Extraire des paires depuis un repo source |
| `/clean-dataset` | Pipeline de nettoyage complet (dédup, filtre, scoring) |
| `/augment-dataset` | Générer des données synthétiques via LLM local |
| `/collect-web` | Veille technologique + collecte de nouvelles paires |
| `/train-lora` | Lancer le fine-tuning LoRA avec config M4 Pro optimale |
| `/eval-model` | Évaluation complète + comparaison baseline |
| `/deploy-model` | Fuse + serve + sanity check + rollback |
| `/compare-models` | Tableau comparatif N modèles sur les mêmes métriques |
| `/daily-report` | Rapport quotidien agrégé (activité, runs, datasets) |
| `/benchmark-all` | Benchmarks complets sur toutes les versions déployées |

---

## Structure des dossiers

```
mlOps/
├── CLAUDE.md                    ← lu par Claude Code à chaque session
├── README.md                    ← ce fichier
├── PYTHON_VERSION.md            ← décision sur Python 3.10 vs 3.11
├── MIGRATION.md                 ← déplacement prévu du repo migrator
├── .gitignore
├── .env.example
├── requirements.txt
├── docker-compose.yml           ← Phase 2 (commenté)
│
├── .claude/
│   ├── settings.json            ← permissions CC + hooks audit
│   ├── audit.log                ← journal horodaté de toutes les actions
│   └── commands/                ← 11 skills (.md par skill)
│
├── datalake/
│   ├── raw/                     ← IMMUABLE après ingestion (versionnée DVC)
│   ├── processed/               ← après extraction et nettoyage initial
│   └── fine-tuning-ready/       ← JSONL prêts pour mlx_lm.lora
│
├── projects/                    ← données structurées par projet
│   ├── migrator/
│   ├── rh/
│   ├── juridique/
│   └── sales/
│
├── models/                      ← modèles fusionnés (gitignore)
├── adapters/                    ← checkpoints LoRA (gitignore)
├── experiments/                 ← runs MLflow + mlops.db SQLite
├── dashboards/                  ← rapports HTML générés automatiquement
│
├── pipeline/                    ← modules Python du pipeline
│   ├── extract.py               ← extraction de paires (tree-sitter)
│   ├── clean.py                 ← nettoyage, scoring, split
│   ├── train.py                 ← wrapper mlx_lm.lora + MLflow
│   ├── evaluate.py              ← métriques, comparaison baseline
│   ├── deploy.py                ← fuse, serve, rollback
│   └── utils/
│       ├── jsonl.py             ← lecture/écriture JSONL + stats
│       ├── metrics.py           ← CodeBLEU, compilation Java
│       └── logger.py            ← logging unifié + MLflow push
│
├── agents/                      ← agents autonomes
│   ├── web_collector.py         ← veille + collecte de paires
│   ├── quality_scorer.py        ← scoring sémantique (Ollama)
│   └── report_generator.py      ← dashboards quotidiens
│
├── config/
│   ├── models/
│   │   ├── mistral-7b.yaml      ← hyperparamètres Mistral 7B
│   │   └── qwen-14b.yaml        ← hyperparamètres Qwen 14B
│   └── projects/
│       └── migrator.yaml        ← config projet Migrator
│
└── tests/
    ├── test_extract.py
    ├── test_clean.py
    └── test_evaluate.py
```

---

## Ajouter un nouveau projet

1. Lancer `/new-project name=<nom> src_lang=<lang> tgt_lang=<lang> model=<modèle>`
2. Pointer sur une source de données : `/extract-pairs project=<nom> repo=<chemin>`
3. Nettoyer : `/clean-dataset project=<nom>`
4. Entraîner : `/train-lora project=<nom>`
5. Évaluer et déployer : `/eval-model` puis `/deploy-model`

Le pipeline est identique pour tous les projets — seules les données changent.

---

## Métriques de succès

Consultables via :
- **MLflow** : `http://localhost:5001` — courbes de loss, hyperparamètres, artefacts
- **Dashboards HTML** : `dashboards/` — rapports quotidiens, évaluations, comparaisons
- **SQLite** : `experiments/mlops.db` — métadonnées projets et runs

Métriques clés par projet (exemple Migrator) :
| Métrique | Baseline (Nova Pro) | Objectif modèle local |
|---|---|---|
| Java compilation rate | ~97% | ≥ 90% |
| CodeBLEU | ~0.76 | ≥ 0.70 |
| Latence p50 | ~1840ms | ≤ 3680ms (2×) |

---

## Principes Green IT

- Tout ce qui peut être automatisé par du code l'est — l'IA intervient uniquement là où elle apporte une vraie valeur ajoutée
- Les configurations Claude Code sont mutualisées dans `~/.claude/settings.json` (global)
- Les modèles locaux remplacent les appels cloud en production
- Réentraînements mensuels autonomes via Prefect (agents/web_collector.py)
