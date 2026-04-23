# /new-project — Initialize a new fine-tuning project

## Usage
```
/new-project name=<project_name> src_lang=<language> tgt_lang=<language> model=<mistral-7b|qwen-14b> classification=<interne|confidentiel|secret>
```

## What this skill does
1. Creates `datalake/projects/<name>/` with subdirs: raw/, processed/, fine-tuning-ready/
2. Creates `projects/<name>/README.md` with project description template
3. Creates `config/projects/<name>.yaml` from the standard template
4. Registers the project in `experiments/mlops.db` (SQLite)
5. Creates a dedicated MLflow experiment: `mlflow experiments create -n <name>`
6. Appends an entry to `CLAUDE.md` under "Active Projects"
7. Generates `projects/<name>/README.md` in French

## Steps for Claude

### Step 1 — Validate arguments
Check that all required args are provided. If not, ask for them explicitly.
Validate `model` is one of: `mistral-7b`, `qwen-14b`.
Validate `classification` is one of: `interne`, `confidentiel`, `secret`.

### Step 2 — Create directory structure
```bash
mkdir -p datalake/projects/<name>/{raw,processed,fine-tuning-ready}
touch datalake/projects/<name>/raw/.gitkeep
touch datalake/projects/<name>/processed/.gitkeep
touch datalake/projects/<name>/fine-tuning-ready/.gitkeep
mkdir -p projects/<name>
```

### Step 3 — Create config file
Generate `config/projects/<name>.yaml` based on `config/projects/migrator.yaml` as template.
Adjust: name, description, source_lang, target_lang, classification, model, serving_port.
Serving port: start at 8080, increment by 1 for each additional project.

### Step 4 — Register in MLflow
```bash
source .venv/bin/activate && mlflow experiments create -n <name>
```

### Step 5 — Register in SQLite
```python
python -c "
import sqlite3
from pathlib import Path
db = sqlite3.connect('experiments/mlops.db')
db.execute('''CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY, src_lang TEXT, tgt_lang TEXT,
    model TEXT, classification TEXT, created_at TEXT
)''')
db.execute('INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,datetime(\"now\"))',
    ('<name>', '<src_lang>', '<tgt_lang>', '<model>', '<classification>'))
db.commit()
db.close()
print('Registered in SQLite')
"
```

### Step 6 — Report
Print a summary table:
- Project name, languages, model, classification level
- Paths created
- MLflow experiment URL: http://localhost:5001/#/experiments/<id>

## Expected output
```
✓ datalake/projects/<name>/ created
✓ config/projects/<name>.yaml created
✓ MLflow experiment registered (id: N)
✓ SQLite entry created
✓ CLAUDE.md updated

Next step: run /extract-pairs project=<name> to populate datalake/projects/<name>/raw/
```
