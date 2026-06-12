"""Prompt variant A/B tester for generate_bilingual.py.

Tests N prompt variants on M sample records, scores each generated pair
with the LLM judge from clean.py, and writes a ranked HTML report.

Usage:
    python -m pipeline.test_prompts --project migrator --adapter jpa-hibernate-to-sqlalchemy-migrator --samples 10
    python -m pipeline.test_prompts --project migrator --adapter spring-config-to-python-config-migrator --samples 10
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.clean import DataCleaner, _syntax_check
from pipeline.utils.jsonl import read_jsonl
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.test_prompts")

_PROJECTS_DIR = Path("config/projects")
_REPORTS_DIR  = Path("dashboards/prompt_tests")


# ── Prompt variant registry ────────────────────────────────────────────────────
# Each variant: {id, label, description, build_fn(src_lang, tgt_lang, code, description, adapter_desc) -> str}

def _v_baseline(src_lang: str, tgt_lang: str, code: str, description: str, adapter_desc: str) -> str:
    return (
        f"You are an expert {src_lang}-to-{tgt_lang} code translator.\n"
        f"Context: {adapter_desc}\n\n"
        f"Task description: {description}\n\n"
        f"Translate the following {src_lang} code to idiomatic {tgt_lang}. "
        f"Output ONLY the {tgt_lang} code, no explanation.\n\n"
        f"```{src_lang}\n{code}\n```"
    )


def _v_migration_context(src_lang: str, tgt_lang: str, code: str, description: str, adapter_desc: str) -> str:
    return (
        f"You are a senior software engineer migrating a production codebase from {src_lang} to {tgt_lang}.\n"
        f"Your task: {adapter_desc}\n\n"
        f"The following {src_lang} code snippet comes from a real migration project.\n"
        f"Context: {description}\n\n"
        f"Produce the equivalent idiomatic {tgt_lang} code that preserves the exact semantics, "
        f"structure, and naming conventions. Use {tgt_lang} best practices and standard libraries.\n"
        f"Output ONLY the {tgt_lang} code block, no explanation, no markdown prose.\n\n"
        f"```{src_lang}\n{code}\n```"
    )


def _v_few_shot(src_lang: str, tgt_lang: str, code: str, description: str, adapter_desc: str) -> str:
    examples = {
        ("python", "java"): (
            "# Python\nclass User:\n    def __init__(self, name: str):\n        self.name = name",
            "// Java\npublic class User {\n    private String name;\n    public User(String name) { this.name = name; }\n}"
        ),
        ("java", "python"): (
            "// Java\n@Entity\npublic class Product {\n    @Id private Long id;\n    private String name;\n}",
            "# Python/SQLAlchemy\nclass Product(Base):\n    __tablename__ = 'product'\n    id = Column(Integer, primary_key=True)\n    name = Column(String)"
        ),
    }
    ex_src, ex_tgt = examples.get((src_lang, tgt_lang), examples.get(("python", "java")))
    return (
        f"You are an expert {src_lang}-to-{tgt_lang} migration specialist.\n"
        f"Task: {adapter_desc}\n\n"
        f"Example:\n"
        f"INPUT ({src_lang}):\n```{src_lang}\n{ex_src}\n```\n"
        f"OUTPUT ({tgt_lang}):\n```{tgt_lang}\n{ex_tgt}\n```\n\n"
        f"Now translate this {src_lang} code to {tgt_lang}.\n"
        f"Context: {description}\n"
        f"Output ONLY the {tgt_lang} code, no explanation.\n\n"
        f"```{src_lang}\n{code}\n```"
    )


def _v_strict_output(src_lang: str, tgt_lang: str, code: str, description: str, adapter_desc: str) -> str:
    return (
        f"TASK: Translate {src_lang} to {tgt_lang}\n"
        f"DOMAIN: {adapter_desc}\n"
        f"CONTEXT: {description}\n"
        f"RULES:\n"
        f"  1. Output ONLY valid {tgt_lang} code\n"
        f"  2. Preserve all logic and variable names\n"
        f"  3. Use idiomatic {tgt_lang} patterns\n"
        f"  4. No explanations, comments, or markdown prose\n\n"
        f"INPUT ({src_lang}):\n{code}\n\n"
        f"OUTPUT ({tgt_lang}):"
    )


VARIANTS: list[dict[str, Any]] = [
    {"id": "v1_baseline",         "label": "Baseline",          "build": _v_baseline},
    {"id": "v2_migration_context","label": "Migration context",  "build": _v_migration_context},
    {"id": "v3_few_shot",         "label": "Few-shot example",   "build": _v_few_shot},
    {"id": "v4_strict_output",    "label": "Strict output fmt",  "build": _v_strict_output},
]


# ── Core ───────────────────────────────────────────────────────────────────────

def _extract_code(response: str, lang: str) -> str:
    import re  # noqa: PLC0415
    m = re.search(rf"```{re.escape(lang)}\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    return response.strip()


def _llm_judge(src_lang: str, tgt_lang: str, src_code: str, tgt_code: str) -> float | None:
    prompt = (
        f"You are a strict code migration quality judge.\n"
        f"Rate this {src_lang}→{tgt_lang} translation from 0 to 10:\n"
        f"- 0-3: incorrect (logic errors, wrong constructs, unrelated code)\n"
        f"- 4-6: partially correct (minor issues, non-idiomatic)\n"
        f"- 7-9: correct and idiomatic\n"
        f"- 10: perfect\n\n"
        f"Reply with a single integer ONLY.\n\n"
        f"SOURCE ({src_lang}):\n```\n{src_code[:600]}\n```\n\n"
        f"TRANSLATION ({tgt_lang}):\n```\n{tgt_code[:600]}\n```"
    )
    try:
        r = call_llm(prompt, max_tokens=4, temperature=0.0)
        if not r:
            return None
        return min(1.0, max(0.0, float(r.strip().split()[0]) / 10.0))
    except Exception:
        return None


def run_test(
    project_id: str,
    adapter_name: str,
    n_samples: int = 10,
    seed: int = 42,
) -> dict[str, Any]:
    cfg_path = _PROJECTS_DIR / f"{project_id}.yaml"
    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)

    transform = cfg.get("transform", {})
    params = transform.get("params", {})
    project_src_lang: str = params.get("src_lang", "python")
    project_tgt_lang: str = params.get("tgt_lang", "java")

    adapter_cfg = next((a for a in cfg.get("adapters", []) if a["name"] == adapter_name), {})
    adapter_desc: str = adapter_cfg.get("description", "")

    clean_raw = Path(f"datalake/projects/{project_id}/processed/clean_raw/{adapter_name}.jsonl")
    if not clean_raw.exists():
        raise FileNotFoundError(f"clean_raw not found: {clean_raw}")

    records = read_jsonl(clean_raw)

    # Auto-detect actual src_lang from available records — adapter may differ from project default
    lang_counts: dict[str, int] = {}
    for r in records:
        lg = (r.get("lang") or "").lower().strip()
        if lg:
            lang_counts[lg] = lang_counts.get(lg, 0) + 1

    # Prefer project src_lang if available, else take the most frequent lang
    if project_src_lang in lang_counts:
        src_lang = project_src_lang
        tgt_lang = project_tgt_lang
    elif lang_counts:
        src_lang = max(lang_counts, key=lambda k: lang_counts[k])
        tgt_lang = project_tgt_lang if project_tgt_lang != src_lang else project_src_lang
    else:
        src_lang = project_src_lang
        tgt_lang = project_tgt_lang

    logger.info("Detected src_lang=%s tgt_lang=%s for adapter '%s'", src_lang, tgt_lang, adapter_name)

    src_records = [r for r in records if (r.get("lang") or "").lower() == src_lang and r.get("code", "").strip()]
    if not src_records:
        # Fallback: take records without lang tag
        src_records = [r for r in records if not r.get("lang") and r.get("code", "").strip()]
    if not src_records:
        raise ValueError(f"No {src_lang} records found for adapter '{adapter_name}'")

    random.seed(seed)
    sample = random.sample(src_records, min(n_samples, len(src_records)))
    logger.info("Testing %d variants on %d samples for '%s'", len(VARIANTS), len(sample), adapter_name)

    results: dict[str, list[dict[str, Any]]] = {v["id"]: [] for v in VARIANTS}

    for i, rec in enumerate(sample):
        code = rec.get("code", "")
        desc = rec.get("description", "")
        logger.info("[%d/%d] sampling record: %s…", i + 1, len(sample), desc[:60])

        for variant in VARIANTS:
            prompt = variant["build"](src_lang, tgt_lang, code, desc, adapter_desc)
            response = call_llm(prompt, max_tokens=1024, temperature=0.1)

            if not response:
                results[variant["id"]].append({"syntax_ok": False, "llm_score": None, "generated": ""})
                continue

            generated = _extract_code(response, tgt_lang)
            syntax_ok, syntax_err = _syntax_check(generated, tgt_lang)
            llm_score = _llm_judge(src_lang, tgt_lang, code, generated)

            results[variant["id"]].append({
                "syntax_ok":  syntax_ok,
                "llm_score":  llm_score,
                "generated":  generated[:300],
                "src_snippet": code[:200],
            })
            logger.info("  [%s] syntax=%s llm=%.2f", variant["label"], syntax_ok, llm_score or 0)

    # Aggregate per variant
    summary: list[dict[str, Any]] = []
    for v in VARIANTS:
        res = results[v["id"]]
        scores = [r["llm_score"] for r in res if r["llm_score"] is not None]
        syntax_rate = sum(1 for r in res if r["syntax_ok"]) / max(len(res), 1)
        avg_llm = sum(scores) / len(scores) if scores else 0.0
        summary.append({
            "id":          v["id"],
            "label":       v["label"],
            "syntax_rate": round(syntax_rate, 2),
            "avg_llm":     round(avg_llm, 3),
            "n_scored":    len(scores),
            "details":     res,
        })

    summary.sort(key=lambda x: x["avg_llm"], reverse=True)
    winner = summary[0]["id"] if summary else None
    logger.info("Winner: %s (avg_llm=%.3f)", summary[0]["label"], summary[0]["avg_llm"])

    return {
        "adapter":   adapter_name,
        "src_lang":  src_lang,
        "tgt_lang":  tgt_lang,
        "n_samples": len(sample),
        "summary":   summary,
        "winner":    winner,
        "ts":        datetime.now(timezone.utc).isoformat(),
    }


def _render_html(result: dict[str, Any], output_path: Path) -> None:
    rows = ""
    for v in result["summary"]:
        winner_badge = " 🏆" if v["id"] == result["winner"] else ""
        tr_style = ' style="background:#e8f5e9"' if v["id"] == result["winner"] else ""
        syntax_pct = f"{v['syntax_rate']*100:.0f}%"
        llm_str = f"{v['avg_llm']:.3f} / 1.0"
        scored_str = f"{v['n_scored']}/{result['n_samples']}"
        rows += (
            f"<tr{tr_style}>"
            f"<td><strong>{v['label']}{winner_badge}</strong></td>"
            f"<td>{syntax_pct}</td>"
            f"<td>{llm_str}</td>"
            f"<td>{scored_str}</td>"
            f"</tr>"
        )

    detail_sections = ""
    for v in result["summary"]:
        detail_sections += f"<h3>{v['label']} ({v['id']})</h3><table>"
        for i, d in enumerate(v["details"]):
            score_str = f"{d['llm_score']:.2f}" if d["llm_score"] is not None else "N/A"
            syntax_str = "✓" if d["syntax_ok"] else "✗"
            detail_sections += (
                f"<tr><td>Sample {i+1}</td>"
                f"<td>Syntax: {syntax_str}</td>"
                f"<td>LLM score: {score_str}</td>"
                f"<td><pre style='max-width:500px;overflow:auto'>{d['generated'][:200]}</pre></td></tr>"
            )
        detail_sections += "</table>"

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Prompt Test — {result['adapter']}</title>
<style>
body{{font-family:monospace;margin:2em;color:#1a1a2e}}
table{{border-collapse:collapse;width:100%;margin:1em 0}}
th,td{{border:1px solid #ccc;padding:8px;text-align:left}}
th{{background:#003366;color:#fff}}
pre{{background:#f5f5f5;padding:6px;font-size:10pt}}
h2{{color:#003366}} h3{{margin-top:1.5em}}
.badge{{background:#2e7d32;color:#fff;padding:2px 8px;border-radius:3px;font-size:9pt}}
</style></head><body>
<h1>Test comparatif de prompts</h1>
<p><strong>Adapter:</strong> {result['adapter']} &nbsp;|&nbsp;
   <strong>Direction:</strong> {result['src_lang']} → {result['tgt_lang']} &nbsp;|&nbsp;
   <strong>Samples:</strong> {result['n_samples']} &nbsp;|&nbsp;
   <strong>Date:</strong> {result['ts'][:10]}</p>
<p><strong>Gagnant:</strong> <span class="badge">{result['summary'][0]['label']}</span>
   (avg LLM judge = {result['summary'][0]['avg_llm']:.3f})</p>

<h2>Résumé</h2>
<table>
  <tr><th>Variant</th><th>Syntaxe OK</th><th>Score LLM moyen</th><th>Paires scorées</th></tr>
  {rows}
</table>

<h2>Détails par sample</h2>
{detail_sections}
</body></html>"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Report written to %s", output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Test prompt variants for generate_bilingual")
    parser.add_argument("--project",  required=True)
    parser.add_argument("--adapter",  required=True)
    parser.add_argument("--samples",  type=int, default=10, dest="n_samples")
    parser.add_argument("--seed",     type=int, default=42)
    args = parser.parse_args()

    result = run_test(args.project, args.adapter, args.n_samples, args.seed)

    report_path = _REPORTS_DIR / f"{args.adapter}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}.html"
    _render_html(result, report_path)

    print(f"\n{'='*60}")
    print(f"Adapter : {result['adapter']}")
    print(f"Samples : {result['n_samples']}")
    print(f"{'Variant':<30} {'Syntax':>8} {'LLM avg':>10}")
    print("-" * 52)
    for v in result["summary"]:
        winner = " ← WINNER" if v["id"] == result["winner"] else ""
        print(f"  {v['label']:<28} {v['syntax_rate']*100:>6.0f}%  {v['avg_llm']:>8.3f}{winner}")
    print(f"\nReport: {report_path}")
    print(f"Winner: {result['summary'][0]['label']} — use this variant in generate_bilingual.py")


if __name__ == "__main__":
    main()
