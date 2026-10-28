"""Compliance audit report generator — NIS2, AI Act, OIV.

Produces a structured compliance report for a delivered model bundle.
The report is deterministic and derived entirely from project metadata
and evaluation results — no LLM involved.

Frameworks covered:
  - NIS2 Directive (EU 2022/2555) — critical infrastructure cybersecurity
  - EU AI Act (2024/1689) — high-risk AI systems
  - OIV (Opérateur d'Importance Vitale, France) — sovereign data handling

Usage:
    from pipeline.utils.compliance import generate_compliance_report, ComplianceStatus

    report = generate_compliance_report(project_cfg, eval_metrics, model_info)
    print(report.summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Status enum ───────────────────────────────────────────────────────────────

class ComplianceStatus:
    PASS    = "pass"      # requirement met
    FAIL    = "fail"      # requirement not met — blocks delivery
    WARNING = "warning"   # partially met or unverifiable — human review needed
    NA      = "n/a"       # not applicable for this project


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class ComplianceCheck:
    id: str                    # e.g. "NIS2-1"
    framework: str             # "NIS2" | "AI_ACT" | "OIV"
    title: str
    description: str
    status: str                # ComplianceStatus
    evidence: str              # what was checked
    recommendation: str = ""   # what to do if status != pass


@dataclass
class ComplianceReport:
    project_id: str
    model_id: str
    generated_at: str
    checks: list[ComplianceCheck] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == ComplianceStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for c in self.checks if c.status == ComplianceStatus.FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for c in self.checks if c.status == ComplianceStatus.WARNING)

    @property
    def delivery_blocked(self) -> bool:
        return self.failed > 0

    def summary(self) -> str:
        status_line = "BLOQUÉ" if self.delivery_blocked else ("AVERTISSEMENTS" if self.warnings else "CONFORME")
        lines = [
            f"Rapport de conformité — {self.project_id} / {self.model_id}",
            f"Généré le : {self.generated_at}",
            f"Statut global : {status_line}  ({self.passed} ✓  {self.warnings} ⚠  {self.failed} ✗)",
            "",
        ]
        for c in self.checks:
            icon = {"pass": "✓", "fail": "✗", "warning": "⚠", "n/a": "—"}.get(c.status, "?")
            lines.append(f"  [{c.framework}] {icon} {c.title}")
            if c.status != ComplianceStatus.PASS and c.recommendation:
                lines.append(f"       → {c.recommendation}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id":       self.project_id,
            "model_id":         self.model_id,
            "generated_at":     self.generated_at,
            "passed":           self.passed,
            "failed":           self.failed,
            "warnings":         self.warnings,
            "delivery_blocked": self.delivery_blocked,
            "checks": [
                {
                    "id":             c.id,
                    "framework":      c.framework,
                    "title":          c.title,
                    "description":    c.description,
                    "status":         c.status,
                    "evidence":       c.evidence,
                    "recommendation": c.recommendation,
                }
                for c in self.checks
            ],
        }


# ── Check library ─────────────────────────────────────────────────────────────

def _check_nis2_local_inference(model_info: dict[str, Any]) -> ComplianceCheck:
    """NIS2 Art. 21 — Security of network and information systems.

    Requirement: inference must not transit through external networks (cloud calls
    in production = potential data exfiltration path for critical infra operators).
    """
    serving = model_info.get("serving", "")
    cloud_serving = any(p in str(serving).lower() for p in ("bedrock", "openai", "azure", "gpt", "claude"))

    return ComplianceCheck(
        id="NIS2-1",
        framework="NIS2",
        title="Inférence 100 % locale en production",
        description="Art. 21 NIS2 : les opérateurs d'infrastructure critique doivent maîtriser les flux de données. Tout appel cloud en inférence de production constitue un flux non maîtrisé.",
        status=ComplianceStatus.FAIL if cloud_serving else ComplianceStatus.PASS,
        evidence=f"Serveur d'inférence déclaré : {serving or 'Ollama (local)'}",
        recommendation="Remplacer les appels cloud par l'inférence locale via Ollama ou mlx_lm." if cloud_serving else "",
    )


def _check_nis2_data_traceability(eval_metrics: dict[str, Any]) -> ComplianceCheck:
    """NIS2 Art. 23 — Incident reporting requires audit trail."""
    has_eval = bool(eval_metrics.get("n"))
    has_mlflow = eval_metrics.get("mlflow_run_id") or eval_metrics.get("run_id")

    if has_eval and has_mlflow:
        status = ComplianceStatus.PASS
        evidence = f"Évaluation tracée : {eval_metrics.get('n')} exemples, run MLflow {eval_metrics.get('mlflow_run_id') or eval_metrics.get('run_id')}"
    elif has_eval:
        status = ComplianceStatus.WARNING
        evidence = f"Évaluation présente ({eval_metrics.get('n')} exemples) mais sans run MLflow associé."
    else:
        status = ComplianceStatus.FAIL
        evidence = "Aucune évaluation enregistrée — traçabilité insuffisante."

    return ComplianceCheck(
        id="NIS2-2",
        framework="NIS2",
        title="Traçabilité des évaluations (audit trail)",
        description="Art. 23 NIS2 : capacité à produire un rapport d'incident avec historique des versions et métriques du modèle.",
        status=status,
        evidence=evidence,
        recommendation="Lier chaque déploiement à un run MLflow avec métriques complètes." if status != ComplianceStatus.PASS else "",
    )


def _check_aiact_high_risk(project_cfg: dict[str, Any]) -> ComplianceCheck:
    """AI Act Annex III — High-risk AI system classification."""
    domain  = str(project_cfg.get("domain", "")).lower()
    classif = str(project_cfg.get("classification", "")).lower()

    HIGH_RISK_DOMAINS = {"defense", "healthcare", "legal", "hr"}
    is_high_risk = domain in HIGH_RISK_DOMAINS or classif in ("secret", "confidentiel")

    if not is_high_risk:
        return ComplianceCheck(
            id="AIACT-1",
            framework="AI_ACT",
            title="Classification du risque AI Act",
            description="Annex III AI Act : systèmes à haut risque (défense, santé, RH, justice).",
            status=ComplianceStatus.PASS,
            evidence=f"Domaine '{domain}' — non classifié haut risque.",
        )

    return ComplianceCheck(
        id="AIACT-1",
        framework="AI_ACT",
        title="Classification du risque AI Act",
        description="Annex III AI Act : ce projet relève d'un domaine à haut risque.",
        status=ComplianceStatus.WARNING,
        evidence=f"Domaine '{domain}', classification '{classif}' → système à haut risque potentiel.",
        recommendation="Constituer le dossier technique (Art. 11) : objectif, données d'entraînement, métriques, limites connues.",
    )


def _check_aiact_human_oversight(eval_metrics: dict[str, Any]) -> ComplianceCheck:
    """AI Act Art. 14 — Human oversight requirement for high-risk systems."""
    pass_rate = eval_metrics.get("pass_rate")
    has_gates = eval_metrics.get("gates_passed") is not None

    if pass_rate is None:
        return ComplianceCheck(
            id="AIACT-2",
            framework="AI_ACT",
            title="Supervision humaine (Art. 14)",
            description="Art. 14 AI Act : les systèmes à haut risque doivent permettre une supervision humaine effective.",
            status=ComplianceStatus.WARNING,
            evidence="Aucune métrique pass_rate disponible — impossible de valider le seuil de supervision.",
            recommendation="Lancer une évaluation complète avant déploiement.",
        )

    threshold = 0.85
    if pass_rate >= threshold:
        status = ComplianceStatus.PASS
        evidence = f"pass_rate = {pass_rate:.1%} ≥ seuil {threshold:.0%}"
        recommendation = ""
    else:
        status = ComplianceStatus.FAIL
        evidence = f"pass_rate = {pass_rate:.1%} < seuil {threshold:.0%}"
        recommendation = f"Le modèle nécessite un taux de réussite ≥ {threshold:.0%} pour une supervision humaine réduite."

    return ComplianceCheck(
        id="AIACT-2",
        framework="AI_ACT",
        title="Supervision humaine (Art. 14)",
        description="Art. 14 AI Act : les systèmes à haut risque doivent permettre une supervision humaine effective.",
        status=status,
        evidence=evidence,
        recommendation=recommendation,
    )


def _check_aiact_transparency(project_cfg: dict[str, Any], model_info: dict[str, Any]) -> ComplianceCheck:
    """AI Act Art. 13 — Transparency and provision of information."""
    has_description = len(str(project_cfg.get("description", "")).strip()) > 50
    has_model       = bool(model_info.get("model_name") or model_info.get("base_model"))
    has_limitations = bool(project_cfg.get("quality_thresholds"))

    score = sum([has_description, has_model, has_limitations])

    if score == 3:
        status = ComplianceStatus.PASS
        evidence = "Description projet, modèle de base et seuils de qualité renseignés."
    elif score >= 2:
        status = ComplianceStatus.WARNING
        evidence = "Documentation partielle."
        missing = []
        if not has_description: missing.append("description projet (> 50 chars)")
        if not has_model:        missing.append("modèle de base")
        if not has_limitations:  missing.append("quality_thresholds dans config")
        evidence += f" Manquant : {', '.join(missing)}."
    else:
        status = ComplianceStatus.FAIL
        evidence = "Documentation insuffisante pour satisfaire l'Art. 13 AI Act."

    return ComplianceCheck(
        id="AIACT-3",
        framework="AI_ACT",
        title="Transparence du système (Art. 13)",
        description="Art. 13 AI Act : le système doit être suffisamment transparent pour permettre aux utilisateurs d'interpréter les sorties.",
        status=status,
        evidence=evidence,
        recommendation="Compléter : description projet, modèle de base, seuils de qualité." if status != ComplianceStatus.PASS else "",
    )


def _check_oiv_air_gap(project_cfg: dict[str, Any], model_info: dict[str, Any]) -> ComplianceCheck:
    """OIV — données souveraines, aucun flux hors périmètre."""
    classif = str(project_cfg.get("classification", "")).lower()
    is_oiv  = classif in ("secret", "confidentiel") or str(project_cfg.get("domain", "")).lower() == "defense"

    if not is_oiv:
        return ComplianceCheck(
            id="OIV-1",
            framework="OIV",
            title="Isolation réseau (air-gap)",
            description="Exigence OIV : aucun flux vers l'extérieur du périmètre sécurisé en production.",
            status=ComplianceStatus.NA,
            evidence=f"Classification '{classif}' — exigence OIV non applicable.",
        )

    bundle_type = str(model_info.get("bundle_type", "")).lower()
    has_docker  = "docker" in bundle_type or model_info.get("docker_bundle")

    return ComplianceCheck(
        id="OIV-1",
        framework="OIV",
        title="Isolation réseau (air-gap)",
        description="Exigence OIV : le bundle livrable doit fonctionner en réseau isolé sans dépendance externe.",
        status=ComplianceStatus.PASS if has_docker else ComplianceStatus.WARNING,
        evidence=f"Bundle type : {bundle_type or 'non spécifié'}. Docker auto-contenu : {'oui' if has_docker else 'non confirmé'}.",
        recommendation="Livrer un bundle Docker auto-contenu (Ollama + Qdrant + Router) sans pull externe." if not has_docker else "",
    )


def _check_oiv_data_sovereignty(project_cfg: dict[str, Any]) -> ComplianceCheck:
    """OIV — données d'entraînement sur périmètre maîtrisé."""
    raw_path = str(project_cfg.get("data", {}).get("raw", ""))
    is_local = raw_path.startswith("datalake/") or raw_path.startswith("/")

    classif = str(project_cfg.get("classification", "")).lower()
    is_oiv  = classif in ("secret", "confidentiel") or str(project_cfg.get("domain", "")).lower() == "defense"

    if not is_oiv:
        return ComplianceCheck(
            id="OIV-2",
            framework="OIV",
            title="Souveraineté des données d'entraînement",
            description="Exigence OIV : les données d'entraînement ne doivent pas quitter le périmètre.",
            status=ComplianceStatus.NA,
            evidence="Non applicable (projet non OIV).",
        )

    return ComplianceCheck(
        id="OIV-2",
        framework="OIV",
        title="Souveraineté des données d'entraînement",
        description="Exigence OIV : les données d'entraînement ne doivent pas quitter le périmètre.",
        status=ComplianceStatus.PASS if is_local else ComplianceStatus.WARNING,
        evidence=f"Chemin raw data : {raw_path or 'non configuré'}",
        recommendation="Vérifier que le dossier datalake/ est sur stockage local non répliqué hors périmètre." if not is_local else "",
    )


# ── Public API ────────────────────────────────────────────────────────────────

def generate_compliance_report(
    project_cfg: dict[str, Any],
    eval_metrics: dict[str, Any],
    model_info: dict[str, Any],
) -> ComplianceReport:
    """Generate a compliance report from project config, eval metrics, and model info.

    Args:
        project_cfg:  Content of config/projects/<id>.yaml
        eval_metrics: Metrics dict from evaluate.py (pass_rate, n, rouge_l, ...)
        model_info:   Model metadata (serving, bundle_type, model_name, ...)

    Returns:
        ComplianceReport with all checks populated.
    """
    checks = [
        _check_nis2_local_inference(model_info),
        _check_nis2_data_traceability(eval_metrics),
        _check_aiact_high_risk(project_cfg),
        _check_aiact_human_oversight(eval_metrics),
        _check_aiact_transparency(project_cfg, model_info),
        _check_oiv_air_gap(project_cfg, model_info),
        _check_oiv_data_sovereignty(project_cfg),
    ]

    return ComplianceReport(
        project_id=str(project_cfg.get("id", project_cfg.get("name", "unknown"))),
        model_id=str(model_info.get("model_id", model_info.get("id", "unknown"))),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        checks=checks,
    )
