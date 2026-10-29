"""Seed mock evaluation data for dashboard development.

Inserts 3 model versions × 20 fictional projects with realistic KPI progressions.
Run: python scripts/seed_mock_evals.py [--reset]
"""

from __future__ import annotations

import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.utils.kpi_store import (
    KpiStore,
    RunRecord,
    CategoryBreakdownItem,
    RobustnessMetrics,
    SecurityMetrics,
    ComplianceMetrics,
)
from api.db import execute, fetchall

_STORE = KpiStore()
random.seed(42)


PROJECTS = [
    # (id, name, sector, domain, strategy, base_pass_rate_v1)
    ("p01-tests-unitaires",   "Tests unitaires Java (BRA)",             "banking",  "code",  "ft",      0.62),
    ("p02-docstrings",        "Docstrings Python internes",             "generic",  "code",  "ft",      0.71),
    ("p03-revue-code",        "Revue de code sécurité (OWASP)",         "banking",  "code",  "ft+rag",  0.55),
    ("p04-legacy-cobol",      "Recherche codebase COBOL",               "banking",  "code",  "rag",     0.78),
    ("p05-adr",               "Fiches ADR Capgemini",                   "generic",  "text",  "ft",      0.68),
    ("p06-schema-archi",      "Questions sur schémas d'architecture",   "generic",  "text",  "rag",     0.82),
    ("p07-appels-offres",     "Réponses appels d'offres (UGAP)",        "generic",  "text",  "ft",      0.57),
    ("p08-catalogue-missions","Recherche références missions",           "generic",  "text",  "rag",     0.85),
    ("p09-pv-recette",        "PV de recette Defense",                  "defense",  "text",  "ft",      0.51),
    ("p10-conformite-nis2",   "Checklist conformité NIS2",              "defense",  "text",  "ft+rag",  0.48),
    ("p11-rapport-pentest",   "Rapport pentest ANSSI/PASSI",            "defense",  "text",  "ft",      0.53),
    ("p12-analyse-cve",       "Analyse CVE sur infra client",           "defense",  "code",  "rag",     0.76),
    ("p13-fiches-poste",      "Fiches de poste RH (CCN)",               "generic",  "text",  "ft",      0.74),
    ("p14-reglement-rh",      "Questions règlement intérieur RH",       "generic",  "text",  "rag",     0.88),
    ("p15-clauses-contrat",   "Clauses contractuelles NDA/SLA",         "legal",    "text",  "ft",      0.60),
    ("p16-conformite-rgpd",   "Conformité RGPD sur DPA",                "legal",    "text",  "ft+rag",  0.44),
    ("p17-facturation",       "Rapports de facturation ERP interne",    "generic",  "text",  "ft",      0.79),
    ("p18-qcm",               "Génération QCM formation",               "generic",  "text",  "ft",      0.83),
    ("p19-catalogue-form",    "Questions catalogue de formations",       "generic",  "text",  "rag",     0.91),
    ("p20-config-mlops",      "Config YAML MLOps (self-referential)",   "generic",  "code",  "ft",      0.58),
]

CATEGORIES_BY_DOMAIN = {
    "code": ["simple", "complexe", "edge-case", "erreur"],
    "text": ["format", "contenu", "références", "synthèse"],
}

VERSIONS = ["v1", "v2", "v3"]
PASS_RATE_BOOST = [0.0, 0.08, 0.05]   # improvement per version
MODELS = {
    "code": "Qwen2.5-Coder-7B-Instruct",
    "text": "Qwen3-8B",
}


def _fake_run_id(project_id: str, version: str) -> str:
    return f"mock-{project_id}-{version}"


def _fake_eval_id(run_id: str) -> str:
    return f"eval-{run_id}"


def _jitter(base: float, sigma: float = 0.03) -> float:
    return round(max(0.01, min(0.99, base + random.gauss(0, sigma))), 4)


def _ts(days_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_project(project_id: str, name: str) -> None:
    existing = fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
    if not existing:
        execute(
            "INSERT INTO projects (id, name, description) VALUES (?, ?, ?)",
            (project_id, name, f"Projet fictif de démonstration — {name}"),
        )


def _ensure_run(run_id: str, project_id: str, version: str, domain: str, days_ago: float) -> None:
    existing = fetchall("SELECT id FROM runs WHERE id = ?", (run_id,))
    if not existing:
        execute(
            """INSERT INTO runs
               (id, project_id, experiment_name, model_name, status, iters, batch_size,
                learning_rate, lora_rank, train_loss, val_loss, adapter_path, started_at, finished_at)
               VALUES (?, ?, ?, ?, 'completed', 1000, 8, 0.0002, 16, ?, ?, ?, ?, ?)""",
            (
                run_id, project_id,
                f"{project_id}-{version}",
                MODELS[domain],
                round(random.uniform(0.8, 2.0), 4),
                round(random.uniform(0.5, 1.2), 4),
                f"adapters/{project_id}/{run_id}/",
                _ts(days_ago + 0.1),
                _ts(days_ago),
            ),
        )


def seed() -> None:
    print("Seeding mock evaluation data...")
    n_evals = 0

    for proj_id, proj_name, sector, domain, strategy, base_rate in PROJECTS:
        _ensure_project(proj_id, proj_name)

        prev_pass_rate: float | None = None

        for v_idx, version in enumerate(VERSIONS):
            days_ago = (len(VERSIONS) - v_idx) * 7.0  # one version per week
            run_id = _fake_run_id(proj_id, version)
            eval_id = _fake_eval_id(run_id)

            _ensure_run(run_id, proj_id, version, domain, days_ago)

            pass_rate = _jitter(base_rate + sum(PASS_RATE_BOOST[:v_idx + 1]))
            rouge_l = _jitter(pass_rate * 0.9)
            latency_p50 = random.uniform(120, 800)
            latency_p95 = latency_p50 * random.uniform(1.5, 2.5)
            latency_p99 = latency_p95 * random.uniform(1.1, 1.4)
            n_samples = random.choice([30, 50, 80, 100])

            from pipeline.utils.stats import wilson_ci  # noqa: PLC0415
            ci = wilson_ci(int(pass_rate * n_samples), n_samples)

            delta = round(pass_rate - prev_pass_rate, 4) if prev_pass_rate is not None else None
            adapter_size = random.uniform(80, 400)  # MB

            record = RunRecord(
                eval_id=eval_id,
                run_id=run_id,
                project_id=proj_id,
                model_version=version,
                eval_type="local",
                created_at=_ts(days_ago),
                pass_rate=pass_rate,
                pass_rate_ci_low=ci.low,
                pass_rate_ci_high=ci.high,
                pass_rate_margin=ci.margin,
                rouge_l=rouge_l,
                exact_match=_jitter(pass_rate * 0.6),
                exec_rate=_jitter(pass_rate * 1.05) if domain == "code" else None,
                latency_p50=round(latency_p50, 1),
                latency_p95=round(latency_p95, 1),
                latency_p99=round(latency_p99, 1),
                tokens_per_second=round(random.uniform(20, 80), 1),
                ttft_ms=round(random.uniform(50, 300), 1),
                vram_peak_gb=round(random.uniform(4, 18), 2),
                n_samples=n_samples,
                sample_size_sufficient=n_samples >= 50,
                delta_pass_rate=delta,
                regression_rate=_jitter(0.05) if v_idx > 0 else None,
                adapter_size_mb=round(adapter_size, 1),
                baseline_run_id=None,
                evaluator_method="exec_python" if domain == "code" else "rouge_l",
                model_path=f"adapters/{proj_id}/{run_id}/",
                notes=f"Mock data — {strategy.upper()} strategy, {sector} sector",
            )
            _STORE.save_evaluation(record)

            # Category breakdown
            cats = CATEGORIES_BY_DOMAIN[domain]
            items = []
            for cat in cats:
                n_cat = n_samples // len(cats)
                pr_cat = _jitter(pass_rate, sigma=0.05)
                k_cat = int(pr_cat * n_cat)
                cat_ci = wilson_ci(k_cat, n_cat)
                items.append(CategoryBreakdownItem(
                    category=cat,
                    n=n_cat,
                    pass_count=k_cat,
                    pass_rate=round(k_cat / n_cat, 4) if n_cat else 0.0,
                    ci_low=cat_ci.low,
                    ci_high=cat_ci.high,
                    rouge_l=_jitter(rouge_l, sigma=0.04),
                ))
            _STORE.save_category_breakdown(eval_id, run_id, proj_id, items)

            # Robustness (v2 and v3 only)
            if v_idx >= 1:
                _STORE.save_robustness(eval_id, run_id, proj_id, RobustnessMetrics(
                    pass_rate_temp0=_jitter(pass_rate, 0.02),
                    pass_rate_temp03=_jitter(pass_rate, 0.03),
                    pass_rate_temp07=_jitter(pass_rate * 0.92, 0.04),
                    consistency_score=_jitter(0.87, 0.05),
                    degradation_rate=_jitter(0.12, 0.04),
                    catastrophic_forgetting=_jitter(0.04, 0.02),
                    n_consistency_prompts=10,
                    n_temp_sweep_prompts=30,
                ))

            # Security (v3 only, Defense/Legal/Banking sectors)
            if v_idx == 2 and sector in ("defense", "legal", "banking"):
                _STORE.save_security(eval_id, run_id, proj_id, SecurityMetrics(
                    hallucination_rate=_jitter(0.06, 0.02),
                    refusal_rate=_jitter(0.02, 0.01),
                    sensitive_data_leakage_rate=_jitter(0.01, 0.005),
                    adversarial_robustness=_jitter(0.88, 0.04),
                    output_determinism=_jitter(0.94, 0.03),
                    n_probes=20,
                ))

            # Compliance (v3 only, Defense/Legal sectors)
            if v_idx == 2 and sector in ("defense", "legal"):
                _STORE.save_compliance(eval_id, run_id, proj_id, ComplianceMetrics(
                    sector=sector,
                    format_compliance_rate=_jitter(0.82, 0.05),
                    regulatory_citation_accuracy=_jitter(0.75, 0.06),
                    pii_detection_rate=_jitter(0.91, 0.04),
                    audit_traceability_score=_STORE.compute_audit_traceability_score(run_id, eval_id),
                    n_format_checks=n_samples,
                ))

            # Learning curve (v1 only)
            if v_idx == 0:
                curve = [{"n_pairs": n, "pass_rate": _jitter(base_rate * (n / 100) ** 0.4),
                          "rouge_l": _jitter(base_rate * (n / 100) ** 0.35)}
                         for n in [10, 25, 50, 75, 100]]
                _STORE.save_learning_curve(eval_id, run_id, proj_id, curve)

            prev_pass_rate = pass_rate
            n_evals += 1

    print(f"Done. {n_evals} evaluations seeded across {len(PROJECTS)} projects.")


if __name__ == "__main__":
    reset = "--reset" in sys.argv
    if reset:
        print("Resetting mock data...")
        for proj_id, *_ in PROJECTS:
            for version in VERSIONS:
                run_id = _fake_run_id(proj_id, version)
                execute("DELETE FROM eval_category_breakdown WHERE run_id = ?", (run_id,))
                execute("DELETE FROM eval_robustness WHERE run_id = ?", (run_id,))
                execute("DELETE FROM eval_security WHERE run_id = ?", (run_id,))
                execute("DELETE FROM eval_compliance WHERE run_id = ?", (run_id,))
                execute("DELETE FROM eval_learning_curve WHERE run_id = ?", (run_id,))
                execute("DELETE FROM evaluations WHERE run_id = ?", (run_id,))
                execute("DELETE FROM runs WHERE id = ?", (run_id,))
            execute("DELETE FROM projects WHERE id = ?", (proj_id,))

    seed()
