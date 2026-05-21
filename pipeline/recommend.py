"""Targeted data recommendations based on coverage analysis.

Reads the coverage report (from pipeline/cluster.py) and generates specific,
actionable collection recommendations per identified gap.

Recommendation types:
  - missing_category  : a whole expected category has no records
  - under_represented : a category has < threshold% of the dataset
  - low_quality_step  : a specific pipeline step rejects too many records (> threshold%)

Each recommendation maps to a specific source type + collector params so the
user can act on it directly from the UI.

CLI:
    python -m pipeline.recommend --project <id> [--adapter <name>]
    python -m pipeline.recommend --report <coverage_report.json> --project <id>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.recommend")

_PROJECTS_DIR = Path("config/projects")

# If a rejection step has > this % of raw records → flag as quality issue
_REJECTION_STEP_THRESHOLD = 0.30
# If a cluster represents < this % of the dataset → flag as under-represented
_CLUSTER_UNDERREP_THRESHOLD = 5.0


def _load_coverage_report(project_id: str, adapter_name: str | None = None) -> dict[str, Any] | None:
    suffix  = f"_{adapter_name}" if adapter_name else ""
    path    = Path(f"datalake/projects/{project_id}/processed/coverage_report{suffix}.json")
    if not path.exists():
        return None
    with path.open() as fh:
        return json.load(fh)


def _load_quality_summary(project_id: str) -> dict[str, Any] | None:
    """Read processed stage stats — n qualified vs n rejected per step."""
    # We reconstruct from the rejection files rather than a dedicated summary file
    proc = Path(f"datalake/projects/{project_id}/processed")
    if not proc.exists():
        return None

    step_names = ["schema", "syntax", "text_quality", "dedup", "relevance"]
    result: dict[str, int] = {}
    for step in step_names:
        p = proc / f"rejected_{step}.jsonl"
        if p.exists():
            with p.open() as fh:
                result[step] = sum(1 for l in fh if l.strip())
        else:
            result[step] = 0

    qualified_path = proc / "qualified.jsonl"
    n_qualified = 0
    if qualified_path.exists():
        with qualified_path.open() as fh:
            n_qualified = sum(1 for l in fh if l.strip())

    return {"rejection_by_step": result, "qualified": n_qualified}


def _load_adapter_sources(project_id: str, adapter_name: str | None = None) -> list[dict[str, Any]]:
    """Load existing source_configs for the adapter — to avoid duplicate recommendations."""
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return []
    with path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    sources: list[dict[str, Any]] = []
    for a in cfg.get("adapters", []):
        if adapter_name and a.get("name") != adapter_name:
            continue
        sources.extend(a.get("source_configs", []))
    return sources


def generate_recommendations(
    project_id: str,
    adapter_name: str | None = None,
    coverage_report: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Generate a prioritised list of data collection recommendations.

    Each recommendation is a dict with:
      priority    : high | medium | low
      type        : missing_category | under_represented | high_rejection | too_few_records
      label       : human-readable title
      detail      : explanation
      suggestion  : what to collect and how (source_config hint)
    """
    report  = coverage_report or _load_coverage_report(project_id, adapter_name)
    quality = _load_quality_summary(project_id)
    existing_sources = _load_adapter_sources(project_id, adapter_name)
    existing_labels  = {s.get("label", "").lower() for s in existing_sources}

    recs: list[dict[str, Any]] = []

    # ── 1. Coverage gaps (missing or under-represented categories) ────────────
    if report:
        for gap in report.get("gaps", []):
            cat = gap.get("category", "")
            if not gap.get("found", True):
                recs.append({
                    "priority":   "high",
                    "type":       "missing_category",
                    "category":   cat,
                    "label":      f"Catégorie manquante : {cat}",
                    "detail":     f"Aucune donnée collectée pour la catégorie '{cat}'.",
                    "suggestion": {
                        "action": "collect",
                        "topic":  cat,
                        "collector_type": "web_crawl",
                        "note":   f"Search for {cat} examples and documentation.",
                    },
                })
            elif gap.get("pct", 100) < _CLUSTER_UNDERREP_THRESHOLD:
                recs.append({
                    "priority":   "medium",
                    "type":       "under_represented",
                    "category":   cat,
                    "label":      f"Sous-représentée : {cat} ({gap.get('pct', 0):.1f}%)",
                    "detail":     f"La catégorie '{cat}' ne représente que {gap.get('pct', 0):.1f}% du dataset.",
                    "suggestion": {
                        "action": "collect_more",
                        "topic":  cat,
                        "collector_type": "web_crawl",
                        "note":   f"Collect more {cat} examples to reach at least 5% of dataset.",
                    },
                })

    # ── 2. High rejection rates per step ─────────────────────────────────────
    if quality:
        raw_approx = quality["qualified"] + sum(quality["rejection_by_step"].values())
        if raw_approx > 0:
            for step, count in quality["rejection_by_step"].items():
                rate = count / raw_approx
                if rate > _REJECTION_STEP_THRESHOLD:
                    recs.append({
                        "priority": "high",
                        "type":     "high_rejection",
                        "step":     step,
                        "label":    f"Taux de rejet élevé : {step} ({rate:.0%})",
                        "detail":   (
                            f"{count} enregistrements rejetés à l'étape '{step}' ({rate:.0%} du total). "
                            "Vérifiez la qualité des sources ou ajustez les seuils dans config/verifiers/registry.yaml."
                        ),
                        "suggestion": {
                            "action": "review_thresholds",
                            "step":   step,
                            "note":   f"Check rejected_{step}.jsonl for patterns. Consider adjusting threshold in verifiers/registry.yaml.",
                        },
                    })

    # ── 3. Too few total qualified records ────────────────────────────────────
    if quality and quality["qualified"] < 50:
        recs.append({
            "priority": "high",
            "type":     "too_few_records",
            "label":    f"Données insuffisantes : {quality['qualified']} qualifiées (seuil : 50)",
            "detail":   (
                f"Seulement {quality['qualified']} enregistrements qualifiés. "
                "L'adapter aura besoin d'au moins 50 données de haute qualité pour être entraîné."
            ),
            "suggestion": {
                "action": "collect_more",
                "note":   "Launch additional collection campaigns to reach the 50-record threshold.",
            },
        })

    # ── 4. No data collected at all ───────────────────────────────────────────
    if quality is None or (quality["qualified"] == 0 and sum(quality.get("rejection_by_step", {}).values()) == 0):
        recs.append({
            "priority": "high",
            "type":     "no_data",
            "label":    "Aucune donnée collectée",
            "detail":   "Lancez la collecte depuis l'onglet Données → Collecter.",
            "suggestion": {"action": "collect", "note": "Start data collection first."},
        })

    # Sort by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: priority_order.get(r["priority"], 3))

    return recs


def run_for_project(project_id: str, adapter_name: str | None = None) -> list[dict[str, Any]]:
    recs = generate_recommendations(project_id, adapter_name)

    # Persist recommendations
    out_dir = Path(f"datalake/projects/{project_id}/processed")
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{adapter_name}" if adapter_name else ""
    out_path = out_dir / f"recommendations{suffix}.json"
    out_path.write_text(json.dumps(recs, ensure_ascii=False, indent=2))
    logger.info("Recommendations written to %s", out_path)

    return recs


def main() -> None:
    parser = argparse.ArgumentParser(description="Targeted data recommendations")
    parser.add_argument("--project",  required=True, help="Project ID")
    parser.add_argument("--adapter",  help="Adapter name (optional)")
    parser.add_argument("--report",   help="Coverage report JSON (auto-loaded if omitted)")
    args = parser.parse_args()

    report = None
    if args.report:
        with open(args.report) as fh:
            report = json.load(fh)

    recs = run_for_project(args.project, args.adapter)

    if not recs:
        print("No recommendations — dataset looks good!")
        return

    priority_icons = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    for r in recs:
        icon = priority_icons.get(r["priority"], "⬜")
        print(f"\n{icon} [{r['priority'].upper()}] {r['label']}")
        print(f"   {r['detail']}")
        if r.get("suggestion"):
            print(f"   → {r['suggestion'].get('note', '')}")


if __name__ == "__main__":
    main()
