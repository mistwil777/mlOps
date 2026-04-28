-- MLOps metadata database schema
-- Used by MCP SQLite server and pipeline scripts

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Projects ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    base_model  TEXT,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ── Datasets ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id),
    stage           TEXT CHECK(stage IN ('raw','processed','fine-tuning-ready')),
    n_pairs         INTEGER,
    n_train         INTEGER,
    n_valid         INTEGER,
    avg_quality     REAL,
    path            TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ── Training runs ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id),
    experiment_name TEXT,
    model_name      TEXT,
    status          TEXT CHECK(status IN ('running','completed','failed','cancelled')),
    iters           INTEGER,
    batch_size      INTEGER,
    learning_rate   REAL,
    lora_rank       INTEGER,
    train_loss      REAL,
    val_loss        REAL,
    duration_s      INTEGER,
    adapter_path    TEXT,
    mlflow_run_id   TEXT,
    started_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at     TEXT
);

-- ── Evaluations ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS evaluations (
    id              TEXT PRIMARY KEY,
    run_id          TEXT REFERENCES runs(id),
    project_id      TEXT REFERENCES projects(id),
    eval_type       TEXT,  -- 'local', 'bedrock_baseline', 'human'
    model_path      TEXT,
    bleu            REAL,
    rouge_l         REAL,
    pass_rate       REAL,   -- % de tests Java qui passent
    latency_ms      INTEGER,
    notes           TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ── Deployed models ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS models (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id),
    run_id          TEXT REFERENCES runs(id),
    fused_path      TEXT,
    serving_port    INTEGER DEFAULT 8080,
    status          TEXT CHECK(status IN ('serving','stopped','error')),
    deployed_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    stopped_at      TEXT
);

-- ── CC session audit (miroir de audit.log en SQL) ─────────────────────────
CREATE TABLE IF NOT EXISTS cc_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    tool        TEXT,   -- BASH, WRITE, READ
    command     TEXT,
    session_id  TEXT
);

-- ── RAG feedback (sol teams → future retraining) ─────────────────────────
CREATE TABLE IF NOT EXISTS rag_feedback (
    id              TEXT PRIMARY KEY,
    project_id      TEXT REFERENCES projects(id),
    content         TEXT NOT NULL,
    domain          TEXT,
    category        TEXT,
    quality_label   TEXT DEFAULT 'validated',
    validated_by    TEXT,
    ingested_into_rag INTEGER DEFAULT 0,  -- 1 once written to LanceDB
    rag_corpus_version TEXT,              -- tag of the training run that used it
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ── Archived runs (soft-delete) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS archived_runs (
    id              TEXT PRIMARY KEY,
    project_id      TEXT,
    experiment_name TEXT,
    model_name      TEXT,
    status          TEXT,
    iters           INTEGER,
    batch_size      INTEGER,
    learning_rate   REAL,
    lora_rank       INTEGER,
    train_loss      REAL,
    val_loss        REAL,
    duration_s      INTEGER,
    adapter_path    TEXT,
    mlflow_run_id   TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    archived_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ── Indexes ───────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_runs_project   ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_started   ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_evals_run      ON evaluations(run_id);
CREATE INDEX IF NOT EXISTS idx_actions_ts     ON cc_actions(ts);
CREATE INDEX IF NOT EXISTS idx_feedback_project ON rag_feedback(project_id);
CREATE INDEX IF NOT EXISTS idx_feedback_ingested ON rag_feedback(ingested_into_rag);

-- ── Seed : projet migrator ────────────────────────────────────────────────
INSERT OR IGNORE INTO projects(id, name, description, base_model)
VALUES (
    'migrator',
    'Python→Java Migration',
    'Fine-tuned Mistral 7B for Python to Java code migration (Capgemini)',
    'mistral-7b-instruct-v0.3-4bit'
);
