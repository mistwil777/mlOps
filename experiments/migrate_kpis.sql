-- KPI schema migration — additive only, no existing tables modified
-- Run: sqlite3 experiments/mlops.db < experiments/migrate_kpis.sql

PRAGMA foreign_keys=ON;

-- ── Extend evaluations with full KPI columns ──────────────────────────────

ALTER TABLE evaluations ADD COLUMN pass_rate_ci_low       REAL;
ALTER TABLE evaluations ADD COLUMN pass_rate_ci_high      REAL;
ALTER TABLE evaluations ADD COLUMN pass_rate_margin       REAL;
ALTER TABLE evaluations ADD COLUMN exact_match            REAL;
ALTER TABLE evaluations ADD COLUMN exec_rate              REAL;
ALTER TABLE evaluations ADD COLUMN mcnemar_p_value        REAL;
ALTER TABLE evaluations ADD COLUMN mcnemar_winner         TEXT;
ALTER TABLE evaluations ADD COLUMN latency_p50            REAL;
ALTER TABLE evaluations ADD COLUMN latency_p95            REAL;
ALTER TABLE evaluations ADD COLUMN latency_p99            REAL;
ALTER TABLE evaluations ADD COLUMN tokens_per_second      REAL;
ALTER TABLE evaluations ADD COLUMN ttft_ms                REAL;
ALTER TABLE evaluations ADD COLUMN vram_peak_gb           REAL;
ALTER TABLE evaluations ADD COLUMN throughput_concurrent  INTEGER;
ALTER TABLE evaluations ADD COLUMN n_samples              INTEGER;
ALTER TABLE evaluations ADD COLUMN sample_size_sufficient INTEGER;  -- 0/1
ALTER TABLE evaluations ADD COLUMN baseline_run_id        TEXT;
ALTER TABLE evaluations ADD COLUMN delta_pass_rate        REAL;
ALTER TABLE evaluations ADD COLUMN regression_rate        REAL;
ALTER TABLE evaluations ADD COLUMN adapter_size_mb        REAL;
ALTER TABLE evaluations ADD COLUMN model_version          TEXT;
ALTER TABLE evaluations ADD COLUMN evaluator_method       TEXT;  -- exec_python|ast_python|rouge_l|heuristic

-- ── Category breakdown (pass_rate per task category) ─────────────────────

CREATE TABLE IF NOT EXISTS eval_category_breakdown (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id     TEXT REFERENCES evaluations(id) ON DELETE CASCADE,
    run_id      TEXT REFERENCES runs(id),
    project_id  TEXT REFERENCES projects(id),
    category    TEXT    NOT NULL,
    n           INTEGER,
    pass_count  INTEGER,
    pass_rate   REAL,
    ci_low      REAL,
    ci_high     REAL,
    rouge_l     REAL,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_cat_eval    ON eval_category_breakdown(eval_id);
CREATE INDEX IF NOT EXISTS idx_cat_project ON eval_category_breakdown(project_id);

-- ── Robustness evaluation ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS eval_robustness (
    id                          TEXT PRIMARY KEY,
    eval_id                     TEXT REFERENCES evaluations(id),
    run_id                      TEXT REFERENCES runs(id),
    project_id                  TEXT REFERENCES projects(id),
    pass_rate_temp0             REAL,   -- T=0.0
    pass_rate_temp03            REAL,   -- T=0.3
    pass_rate_temp07            REAL,   -- T=0.7
    consistency_score           REAL,   -- mean(mode_count/n_repeats) over prompts
    degradation_rate            REAL,   -- pass_rate_indomain - pass_rate_ood
    catastrophic_forgetting     REAL,   -- pass_rate_base_bench - pass_rate_finetuned
    n_consistency_prompts       INTEGER,
    n_temp_sweep_prompts        INTEGER,
    created_at                  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_rob_run     ON eval_robustness(run_id);
CREATE INDEX IF NOT EXISTS idx_rob_project ON eval_robustness(project_id);

-- ── Security evaluation ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS eval_security (
    id                              TEXT PRIMARY KEY,
    eval_id                         TEXT REFERENCES evaluations(id),
    run_id                          TEXT REFERENCES runs(id),
    project_id                      TEXT REFERENCES projects(id),
    hallucination_rate              REAL,   -- % outputs factually incorrect but confident
    refusal_rate                    REAL,   -- % inappropriate refusals
    sensitive_data_leakage_rate     REAL,   -- % training data reproduced verbatim
    adversarial_robustness          REAL,   -- pass_rate_perturbed / pass_rate_original
    output_determinism              REAL,   -- 1.0 = perfectly deterministic at T=0
    n_probes                        INTEGER,
    created_at                      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_sec_run     ON eval_security(run_id);
CREATE INDEX IF NOT EXISTS idx_sec_project ON eval_security(project_id);

-- ── Compliance evaluation (sector-specific) ───────────────────────────────

CREATE TABLE IF NOT EXISTS eval_compliance (
    id                              TEXT PRIMARY KEY,
    eval_id                         TEXT REFERENCES evaluations(id),
    run_id                          TEXT REFERENCES runs(id),
    project_id                      TEXT REFERENCES projects(id),
    sector                          TEXT CHECK(sector IN ('defense','banking','legal','generic')),
    format_compliance_rate          REAL,   -- % outputs matching required format
    regulatory_citation_accuracy    REAL,   -- accuracy of regulatory refs cited
    pii_detection_rate              REAL,   -- recall on PII detection task
    audit_traceability_score        REAL,   -- 0-1 computed from metadata completeness
    n_format_checks                 INTEGER,
    created_at                      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_comp_run     ON eval_compliance(run_id);
CREATE INDEX IF NOT EXISTS idx_comp_project ON eval_compliance(project_id);

-- ── Learning curve (pass_rate vs n_training_pairs) ────────────────────────

CREATE TABLE IF NOT EXISTS eval_learning_curve (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id     TEXT REFERENCES evaluations(id),
    run_id      TEXT REFERENCES runs(id),
    project_id  TEXT REFERENCES projects(id),
    n_pairs     INTEGER NOT NULL,
    pass_rate   REAL    NOT NULL,
    rouge_l     REAL,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_lc_project ON eval_learning_curve(project_id);
CREATE INDEX IF NOT EXISTS idx_lc_run     ON eval_learning_curve(run_id);
