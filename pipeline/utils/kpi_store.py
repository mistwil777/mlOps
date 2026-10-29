"""KPI persistence layer — reads/writes all evaluation KPIs to SQLite.

All tables are defined in experiments/migrate_kpis.sql.
This module is the single point of contact between pipeline code and the KPI schema.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_DB_PATH = Path("experiments/mlops.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    eval_id: str
    run_id: str
    project_id: str
    model_version: str
    eval_type: str
    created_at: str
    pass_rate: float | None = None
    pass_rate_ci_low: float | None = None
    pass_rate_ci_high: float | None = None
    pass_rate_margin: float | None = None
    rouge_l: float | None = None
    exact_match: float | None = None
    exec_rate: float | None = None
    latency_p50: float | None = None
    latency_p95: float | None = None
    latency_p99: float | None = None
    tokens_per_second: float | None = None
    ttft_ms: float | None = None
    vram_peak_gb: float | None = None
    n_samples: int | None = None
    sample_size_sufficient: bool | None = None
    delta_pass_rate: float | None = None
    regression_rate: float | None = None
    adapter_size_mb: float | None = None
    baseline_run_id: str | None = None
    evaluator_method: str | None = None
    model_path: str | None = None
    notes: str | None = None


@dataclass
class CategoryBreakdownItem:
    category: str
    n: int
    pass_count: int
    pass_rate: float
    ci_low: float
    ci_high: float
    rouge_l: float | None = None


@dataclass
class ProjectKpiSummary:
    project_id: str
    project_name: str
    sector: str | None
    last_eval_id: str | None
    last_run_id: str | None
    last_eval_at: str | None
    pass_rate: float | None
    pass_rate_ci_low: float | None
    pass_rate_ci_high: float | None
    rouge_l: float | None
    latency_p95: float | None
    delta_pass_rate: float | None
    n_runs: int
    quality_gate_pass: bool | None


@dataclass
class KpiHistoryPoint:
    eval_id: str
    run_id: str
    model_version: str | None
    created_at: str
    pass_rate: float | None
    pass_rate_ci_low: float | None
    pass_rate_ci_high: float | None
    rouge_l: float | None
    latency_p95: float | None
    delta_pass_rate: float | None
    n_samples: int | None


@dataclass
class RobustnessMetrics:
    pass_rate_temp0: float | None = None
    pass_rate_temp03: float | None = None
    pass_rate_temp07: float | None = None
    consistency_score: float | None = None
    degradation_rate: float | None = None
    catastrophic_forgetting: float | None = None
    n_consistency_prompts: int | None = None
    n_temp_sweep_prompts: int | None = None


@dataclass
class SecurityMetrics:
    hallucination_rate: float | None = None
    refusal_rate: float | None = None
    sensitive_data_leakage_rate: float | None = None
    adversarial_robustness: float | None = None
    output_determinism: float | None = None
    n_probes: int | None = None


@dataclass
class ComplianceMetrics:
    sector: str = "generic"
    format_compliance_rate: float | None = None
    regulatory_citation_accuracy: float | None = None
    pii_detection_rate: float | None = None
    audit_traceability_score: float | None = None
    n_format_checks: int | None = None


# ── KpiStore ──────────────────────────────────────────────────────────────────

class KpiStore:
    """Read/write interface for all KPI tables."""

    def __init__(self, db_path: Path = _DB_PATH):
        self._db = db_path

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ── Write ─────────────────────────────────────────────────────────────────

    def save_evaluation(self, record: RunRecord) -> str:
        """Upsert an evaluation record. Returns eval_id."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO evaluations (
                    id, run_id, project_id, eval_type, model_path, model_version,
                    pass_rate, pass_rate_ci_low, pass_rate_ci_high, pass_rate_margin,
                    rouge_l, exact_match, exec_rate,
                    latency_p50, latency_p95, latency_p99,
                    tokens_per_second, ttft_ms, vram_peak_gb,
                    n_samples, sample_size_sufficient,
                    delta_pass_rate, regression_rate, adapter_size_mb,
                    baseline_run_id, evaluator_method, notes
                ) VALUES (
                    :eval_id, :run_id, :project_id, :eval_type, :model_path, :model_version,
                    :pass_rate, :pass_rate_ci_low, :pass_rate_ci_high, :pass_rate_margin,
                    :rouge_l, :exact_match, :exec_rate,
                    :latency_p50, :latency_p95, :latency_p99,
                    :tokens_per_second, :ttft_ms, :vram_peak_gb,
                    :n_samples, :sample_size_sufficient,
                    :delta_pass_rate, :regression_rate, :adapter_size_mb,
                    :baseline_run_id, :evaluator_method, :notes
                )
                """,
                {
                    "eval_id": record.eval_id,
                    "run_id": record.run_id,
                    "project_id": record.project_id,
                    "eval_type": record.eval_type,
                    "model_path": record.model_path,
                    "model_version": record.model_version,
                    "pass_rate": record.pass_rate,
                    "pass_rate_ci_low": record.pass_rate_ci_low,
                    "pass_rate_ci_high": record.pass_rate_ci_high,
                    "pass_rate_margin": record.pass_rate_margin,
                    "rouge_l": record.rouge_l,
                    "exact_match": record.exact_match,
                    "exec_rate": record.exec_rate,
                    "latency_p50": record.latency_p50,
                    "latency_p95": record.latency_p95,
                    "latency_p99": record.latency_p99,
                    "tokens_per_second": record.tokens_per_second,
                    "ttft_ms": record.ttft_ms,
                    "vram_peak_gb": record.vram_peak_gb,
                    "n_samples": record.n_samples,
                    "sample_size_sufficient": int(record.sample_size_sufficient) if record.sample_size_sufficient is not None else None,
                    "delta_pass_rate": record.delta_pass_rate,
                    "regression_rate": record.regression_rate,
                    "adapter_size_mb": record.adapter_size_mb,
                    "baseline_run_id": record.baseline_run_id,
                    "evaluator_method": record.evaluator_method,
                    "notes": record.notes,
                },
            )
        return record.eval_id

    def save_category_breakdown(
        self, eval_id: str, run_id: str, project_id: str, items: list[CategoryBreakdownItem]
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM eval_category_breakdown WHERE eval_id = ?", (eval_id,)
            )
            conn.executemany(
                """
                INSERT INTO eval_category_breakdown
                    (eval_id, run_id, project_id, category, n, pass_count, pass_rate, ci_low, ci_high, rouge_l)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (eval_id, run_id, project_id, it.category, it.n, it.pass_count,
                     it.pass_rate, it.ci_low, it.ci_high, it.rouge_l)
                    for it in items
                ],
            )

    def save_robustness(
        self, eval_id: str, run_id: str, project_id: str, metrics: RobustnessMetrics
    ) -> str:
        rid = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_robustness (
                    id, eval_id, run_id, project_id,
                    pass_rate_temp0, pass_rate_temp03, pass_rate_temp07,
                    consistency_score, degradation_rate, catastrophic_forgetting,
                    n_consistency_prompts, n_temp_sweep_prompts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, eval_id, run_id, project_id,
                    metrics.pass_rate_temp0, metrics.pass_rate_temp03, metrics.pass_rate_temp07,
                    metrics.consistency_score, metrics.degradation_rate, metrics.catastrophic_forgetting,
                    metrics.n_consistency_prompts, metrics.n_temp_sweep_prompts,
                ),
            )
        return rid

    def save_security(
        self, eval_id: str, run_id: str, project_id: str, metrics: SecurityMetrics
    ) -> str:
        rid = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_security (
                    id, eval_id, run_id, project_id,
                    hallucination_rate, refusal_rate, sensitive_data_leakage_rate,
                    adversarial_robustness, output_determinism, n_probes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, eval_id, run_id, project_id,
                    metrics.hallucination_rate, metrics.refusal_rate,
                    metrics.sensitive_data_leakage_rate, metrics.adversarial_robustness,
                    metrics.output_determinism, metrics.n_probes,
                ),
            )
        return rid

    def save_compliance(
        self, eval_id: str, run_id: str, project_id: str, metrics: ComplianceMetrics
    ) -> str:
        rid = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO eval_compliance (
                    id, eval_id, run_id, project_id, sector,
                    format_compliance_rate, regulatory_citation_accuracy,
                    pii_detection_rate, audit_traceability_score, n_format_checks
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid, eval_id, run_id, project_id, metrics.sector,
                    metrics.format_compliance_rate, metrics.regulatory_citation_accuracy,
                    metrics.pii_detection_rate, metrics.audit_traceability_score,
                    metrics.n_format_checks,
                ),
            )
        return rid

    def save_learning_curve(
        self, eval_id: str, run_id: str, project_id: str, points: list[dict[str, Any]]
    ) -> None:
        """points: [{"n_pairs": int, "pass_rate": float, "rouge_l": float|None}]"""
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM eval_learning_curve WHERE eval_id = ?", (eval_id,)
            )
            conn.executemany(
                """
                INSERT INTO eval_learning_curve (eval_id, run_id, project_id, n_pairs, pass_rate, rouge_l)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [(eval_id, run_id, project_id, p["n_pairs"], p["pass_rate"], p.get("rouge_l"))
                 for p in points],
            )

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_all_projects_summary(self) -> list[ProjectKpiSummary]:
        """One row per project: latest eval KPIs + delta vs previous."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.id               AS project_id,
                    p.name             AS project_name,
                    p.sector           AS sector,
                    e.id               AS last_eval_id,
                    e.run_id           AS last_run_id,
                    e.created_at       AS last_eval_at,
                    e.pass_rate,
                    e.pass_rate_ci_low,
                    e.pass_rate_ci_high,
                    e.rouge_l,
                    e.latency_p95,
                    e.delta_pass_rate,
                    (SELECT COUNT(*) FROM evaluations e3 WHERE e3.project_id = p.id) AS n_runs
                FROM projects p
                LEFT JOIN evaluations e ON e.id = (
                    SELECT e2.id FROM evaluations e2
                    WHERE e2.project_id = p.id
                    ORDER BY e2.created_at DESC, e2.id DESC
                    LIMIT 1
                )
                ORDER BY p.name
                """
            ).fetchall()
        return [
            ProjectKpiSummary(
                project_id=r["project_id"],
                project_name=r["project_name"],
                sector=r["sector"],
                last_eval_id=r["last_eval_id"],
                last_run_id=r["last_run_id"],
                last_eval_at=r["last_eval_at"],
                pass_rate=r["pass_rate"],
                pass_rate_ci_low=r["pass_rate_ci_low"],
                pass_rate_ci_high=r["pass_rate_ci_high"],
                rouge_l=r["rouge_l"],
                latency_p95=r["latency_p95"],
                delta_pass_rate=r["delta_pass_rate"],
                n_runs=r["n_runs"] or 0,
                quality_gate_pass=bool(r["pass_rate"] and r["pass_rate"] >= 0.7) if r["pass_rate"] is not None else None,
            )
            for r in rows
        ]

    def get_kpi_history(self, project_id: str, limit: int = 20) -> list[KpiHistoryPoint]:
        """Chronological list of eval KPIs for a project."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, run_id, model_version, created_at,
                       pass_rate, pass_rate_ci_low, pass_rate_ci_high,
                       rouge_l, latency_p95, delta_pass_rate, n_samples
                FROM evaluations
                WHERE project_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (project_id, limit),
            ).fetchall()
        return [
            KpiHistoryPoint(
                eval_id=r["id"],
                run_id=r["run_id"],
                model_version=r["model_version"],
                created_at=r["created_at"],
                pass_rate=r["pass_rate"],
                pass_rate_ci_low=r["pass_rate_ci_low"],
                pass_rate_ci_high=r["pass_rate_ci_high"],
                rouge_l=r["rouge_l"],
                latency_p95=r["latency_p95"],
                delta_pass_rate=r["delta_pass_rate"],
                n_samples=r["n_samples"],
            )
            for r in rows
        ]

    def get_latest_eval(self, project_id: str) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM evaluations
                WHERE project_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_run_record(row)

    def get_eval(self, eval_id: str) -> RunRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM evaluations WHERE id = ?", (eval_id,)
            ).fetchone()
        return _row_to_run_record(row) if row else None

    def get_robustness(self, eval_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM eval_robustness WHERE eval_id = ? ORDER BY created_at DESC LIMIT 1",
                (eval_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_security(self, eval_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM eval_security WHERE eval_id = ? ORDER BY created_at DESC LIMIT 1",
                (eval_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_compliance(self, eval_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM eval_compliance WHERE eval_id = ? ORDER BY created_at DESC LIMIT 1",
                (eval_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_category_breakdown(self, eval_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_category_breakdown WHERE eval_id = ? ORDER BY category",
                (eval_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_learning_curve(self, project_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT n_pairs, pass_rate, rouge_l, created_at
                FROM eval_learning_curve
                WHERE project_id = ?
                ORDER BY n_pairs
                """,
                (project_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def compute_delta(self, project_id: str, current_eval_id: str) -> float | None:
        """Pass rate delta: current - previous eval for this project."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT pass_rate FROM evaluations
                WHERE project_id = ? AND id != ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (project_id, current_eval_id),
            ).fetchone()
            current = conn.execute(
                "SELECT pass_rate FROM evaluations WHERE id = ?", (current_eval_id,)
            ).fetchone()
        if rows and current and rows["pass_rate"] is not None and current["pass_rate"] is not None:
            return round(current["pass_rate"] - rows["pass_rate"], 4)
        return None

    def compute_audit_traceability_score(self, run_id: str, eval_id: str) -> float:
        """0.25 per criterion: dataset versioned, mlflow_run_id, eval persisted, adapter_path."""
        with self._conn() as conn:
            run = conn.execute(
                "SELECT mlflow_run_id, adapter_path FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            has_eval = conn.execute(
                "SELECT 1 FROM evaluations WHERE id = ?", (eval_id,)
            ).fetchone()
            has_dataset = conn.execute(
                "SELECT 1 FROM datasets WHERE project_id = ("
                "SELECT project_id FROM runs WHERE id = ?) "
                "AND stage = 'fine-tuning-ready'",
                (run_id,),
            ).fetchone()
        score = 0.0
        if run and run["mlflow_run_id"]:
            score += 0.25
        if run and run["adapter_path"]:
            score += 0.25
        if has_eval:
            score += 0.25
        if has_dataset:
            score += 0.25
        return score

    def patch_perf(self, run_id: str, metrics: dict[str, Any]) -> None:
        """Update tokens_per_second, ttft_ms, adapter_size_mb on an existing eval row."""
        eval_id = f"eval-{run_id}"
        with self._conn() as conn:
            exists = conn.execute(
                "SELECT 1 FROM evaluations WHERE id = ?", (eval_id,)
            ).fetchone()
            if not exists:
                return
            conn.execute(
                """
                UPDATE evaluations
                SET tokens_per_second = COALESCE(?, tokens_per_second),
                    ttft_ms           = COALESCE(?, ttft_ms),
                    adapter_size_mb   = COALESCE(?, adapter_size_mb)
                WHERE id = ?
                """,
                (
                    metrics.get("tokens_per_second"),
                    metrics.get("ttft_ms"),
                    metrics.get("adapter_size_mb"),
                    eval_id,
                ),
            )


# ── Helper ────────────────────────────────────────────────────────────────────

def _row_to_run_record(row: sqlite3.Row) -> RunRecord:
    d = dict(row)
    return RunRecord(
        eval_id=d["id"],
        run_id=d.get("run_id", ""),
        project_id=d.get("project_id", ""),
        model_version=d.get("model_version") or "v1",
        eval_type=d.get("eval_type", "local"),
        created_at=d.get("created_at", ""),
        pass_rate=d.get("pass_rate"),
        pass_rate_ci_low=d.get("pass_rate_ci_low"),
        pass_rate_ci_high=d.get("pass_rate_ci_high"),
        pass_rate_margin=d.get("pass_rate_margin"),
        rouge_l=d.get("rouge_l"),
        exact_match=d.get("exact_match"),
        exec_rate=d.get("exec_rate"),
        latency_p50=d.get("latency_p50"),
        latency_p95=d.get("latency_p95"),
        latency_p99=d.get("latency_p99"),
        tokens_per_second=d.get("tokens_per_second"),
        ttft_ms=d.get("ttft_ms"),
        vram_peak_gb=d.get("vram_peak_gb"),
        n_samples=d.get("n_samples"),
        sample_size_sufficient=bool(d["sample_size_sufficient"]) if d.get("sample_size_sufficient") is not None else None,
        delta_pass_rate=d.get("delta_pass_rate"),
        regression_rate=d.get("regression_rate"),
        adapter_size_mb=d.get("adapter_size_mb"),
        baseline_run_id=d.get("baseline_run_id"),
        evaluator_method=d.get("evaluator_method"),
        model_path=d.get("model_path"),
        notes=d.get("notes"),
    )
