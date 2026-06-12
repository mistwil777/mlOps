"""Hyperparameter tuner for the bilingual generation pipeline.

Tests a grid of threshold combinations on a sample of clean_raw records,
scores generated pairs with an independent LLM judge, selects the optimal
combination (maximising avg_score × retention_rate), and optionally writes
the best thresholds into the adapter YAML.

Fully config-driven: works for any project/adapter without hardcoded keywords.

Usage:
    python -m pipeline.tune_pipeline \\
        --project migrator \\
        --adapter jpa-hibernate-to-sqlalchemy-migrator \\
        --samples 50 \\
        --gen-per-config 5
    # Add --no-write to produce a report only, without patching the YAML.
"""

from __future__ import annotations

import argparse
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.clean import _syntax_check
from pipeline.clean_raw import _code_score, _cosine, _load_project_config, _text_score
from pipeline.utils.embeddings import embed as _embed
from pipeline.utils.jsonl import read_jsonl
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.tune_pipeline")

_PROJECTS_DIR = Path("config/projects")
_REPORTS_DIR  = Path("dashboards/tune")

GRID: dict[str, list[float]] = {
    "relevance_threshold":     [0.05, 0.15, 0.25, 0.35, 0.45],
    "bilingual_sim_threshold": [0.10, 0.20, 0.30, 0.40],
    "quality_threshold":       [0.45, 0.55, 0.65],
}


# ── Utilities ──────────────────────────────────────────────────────────────────

def _sample_records(records: list[dict[str, Any]], n: int, seed: int = 42) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    return rng.sample(records, min(n, len(records)))


def _extract_code(response: str, lang: str) -> str:
    m = re.search(rf"```{re.escape(lang)}\n(.*?)```", response, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    return response.strip()


def _build_prompt(src_lang: str, tgt_lang: str, code: str, description: str, adapter_desc: str) -> str:
    return (
        f"You are a senior software engineer migrating a production codebase from {src_lang} to {tgt_lang}.\n"
        f"Your task: {adapter_desc}\n\n"
        f"Context: {description}\n\n"
        f"Produce the equivalent idiomatic {tgt_lang} code that preserves the exact semantics. "
        f"Use {tgt_lang} best practices. Output ONLY the {tgt_lang} code, no explanation.\n\n"
        f"```{src_lang}\n{code}\n```"
    )


def _llm_judge(src_lang: str, tgt_lang: str, src_code: str, tgt_code: str) -> float | None:
    prompt = (
        f"You are a strict code migration quality judge.\n"
        f"Rate this {src_lang}->>{tgt_lang} translation from 0 to 10:\n"
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
    except Exception:  # noqa: BLE001
        return None


def _fscore(avg_score: float, retention_rate: float) -> float:
    return avg_score * retention_rate


# ── Core grid step ─────────────────────────────────────────────────────────────

def _eval_config(
    src_records: list[dict[str, Any]],
    vecs: list[list[float]],
    adapter_vec: list[float],
    rel_thr: float,
    bil_thr: float,
    q_thr: float,
    src_lang: str,
    tgt_lang: str,
    adapter_desc: str,
    n_gen: int,
    domain: str,
) -> dict[str, Any]:
    total = len(src_records)
    if total == 0:
        return {"retention": 0.0, "avg_score": 0.0, "syntax_rate": 0.0, "fscore": 0.0, "n_gen": 0}

    scorer = _code_score if domain == "code" else _text_score

    # Step 1 — quality filter (recalculate since _heuristic_score is stripped on disk)
    q_filtered = [
        (r, v) for r, v in zip(src_records, vecs)
        if scorer(r) >= q_thr
    ]

    # Step 2 — relevance filter (cosine sim record vs adapter description)
    rel_filtered = [
        (r, v) for r, v in q_filtered
        if _cosine(v, adapter_vec) >= rel_thr
    ]

    # Step 3 — bilingual sim filter (same vectors, same reference)
    bil_filtered = [
        (r, v) for r, v in rel_filtered
        if _cosine(v, adapter_vec) >= bil_thr
    ]

    retained = len(bil_filtered)
    retention_rate = retained / max(total, 1)

    if retained == 0:
        return {"retention": retention_rate, "avg_score": 0.0, "syntax_rate": 0.0, "fscore": 0.0, "n_gen": 0}

    # Step 4 — generate + judge on up to n_gen records
    rng = random.Random(42)
    sample = rng.sample(bil_filtered, min(n_gen, retained))

    scores, syntax_ok_count = [], 0
    for rec, _ in sample:
        code = rec.get("code", "")
        desc = rec.get("description", "")
        prompt = _build_prompt(src_lang, tgt_lang, code, desc, adapter_desc)
        response = call_llm(prompt, max_tokens=1024, temperature=0.1)
        if not response:
            continue
        generated = _extract_code(response, tgt_lang)
        if len(generated) < 20:
            continue
        syntax_ok, _ = _syntax_check(generated, tgt_lang)
        if syntax_ok:
            syntax_ok_count += 1
        s = _llm_judge(src_lang, tgt_lang, code, generated)
        if s is not None:
            scores.append(s)

    avg_score = sum(scores) / len(scores) if scores else 0.0
    syntax_rate = syntax_ok_count / len(sample) if sample else 0.0
    fs = _fscore(avg_score, retention_rate)

    return {
        "retention":    round(retention_rate, 4),
        "avg_score":    round(avg_score, 4),
        "syntax_rate":  round(syntax_rate, 4),
        "fscore":       round(fs, 4),
        "n_gen":        len(scores),
    }


# ── Main tuner ─────────────────────────────────────────────────────────────────

def run_tuner(
    project_id: str,
    adapter_name: str,
    n_samples: int = 50,
    n_gen_per_config: int = 5,
    seed: int = 42,
) -> dict[str, Any]:
    cfg = _load_project_config(project_id)
    adapter = next((a for a in cfg.get("adapters", []) if a["name"] == adapter_name), None)
    if adapter is None:
        raise ValueError(f"Adapter '{adapter_name}' not found in project '{project_id}'")

    adapter_desc: str = adapter.get("description", "")
    domain: str = cfg.get("domain", "code")

    transform = cfg.get("transform", {})
    params = transform.get("params", {})
    project_src_lang: str = params.get("src_lang", "python")
    project_tgt_lang: str = params.get("tgt_lang", "java")

    clean_raw_path = Path(f"datalake/projects/{project_id}/processed/clean_raw/{adapter_name}.jsonl")
    if not clean_raw_path.exists():
        raise FileNotFoundError(f"clean_raw not found: {clean_raw_path}")

    all_records = read_jsonl(clean_raw_path)

    # Auto-detect src_lang from record distribution
    lang_counts: dict[str, int] = {}
    for r in all_records:
        lg = (r.get("lang") or "").lower().strip()
        if lg:
            lang_counts[lg] = lang_counts.get(lg, 0) + 1

    if project_src_lang in lang_counts:
        src_lang, tgt_lang = project_src_lang, project_tgt_lang
    elif lang_counts:
        src_lang = max(lang_counts, key=lambda k: lang_counts[k])
        tgt_lang = project_tgt_lang if project_tgt_lang != src_lang else project_src_lang
    else:
        src_lang, tgt_lang = project_src_lang, project_tgt_lang

    logger.info("Adapter '%s': src_lang=%s tgt_lang=%s domain=%s", adapter_name, src_lang, tgt_lang, domain)

    src_records = [r for r in all_records if (r.get("lang") or "").lower() == src_lang and r.get("code", "").strip()]
    if not src_records:
        src_records = [r for r in all_records if r.get("code", "").strip()]

    sample = _sample_records(src_records, n_samples, seed)
    logger.info("Sampled %d/%d records for tuning", len(sample), len(src_records))

    # Embed once — reuse for all grid iterations
    logger.info("Embedding %d records + adapter description…", len(sample))
    texts = [(r.get("description", "") + " " + str(r.get("code", ""))[:400]) for r in sample]
    vecs = _embed(texts)
    adapter_vec_list = _embed([adapter_desc[:500]])
    if not adapter_vec_list or not vecs:
        raise RuntimeError("Embedding failed — check Bedrock/Ollama availability")
    adapter_vec = adapter_vec_list[0]

    # Grid search
    rel_values = GRID["relevance_threshold"]
    bil_values = GRID["bilingual_sim_threshold"]
    q_values   = GRID["quality_threshold"]
    total_configs = len(rel_values) * len(bil_values) * len(q_values)
    logger.info("Grid: %d combinations (%d rel × %d bil × %d q)",
                total_configs, len(rel_values), len(bil_values), len(q_values))

    results: list[dict[str, Any]] = []
    idx = 0
    for rel_thr in rel_values:
        for bil_thr in bil_values:
            for q_thr in q_values:
                idx += 1
                metrics = _eval_config(
                    sample, vecs, adapter_vec,
                    rel_thr, bil_thr, q_thr,
                    src_lang, tgt_lang, adapter_desc,
                    n_gen_per_config, domain,
                )
                entry = {
                    "relevance_threshold":     rel_thr,
                    "bilingual_sim_threshold": bil_thr,
                    "quality_threshold":       q_thr,
                    **metrics,
                }
                results.append(entry)
                logger.info(
                    "[%d/%d] rel=%.2f bil=%.2f q=%.2f → ret=%.0f%% avg=%.2f fscore=%.3f",
                    idx, total_configs,
                    rel_thr, bil_thr, q_thr,
                    metrics["retention"] * 100, metrics["avg_score"], metrics["fscore"],
                )

    best = max(results, key=lambda r: r["fscore"])
    results_sorted = sorted(results, key=lambda r: r["fscore"], reverse=True)

    # Alert if best config has high retention but low quality
    if best["retention"] > 0.8 and best["avg_score"] < 0.6:
        logger.warning(
            "ALERT: Best config has high retention (%.0f%%) but low avg score (%.2f) — "
            "thresholds may be too permissive. Consider lowering n_gen_per_config for more signal.",
            best["retention"] * 100, best["avg_score"],
        )

    logger.info(
        "Best: rel=%.2f bil=%.2f q=%.2f → fscore=%.3f (ret=%.0f%% avg=%.2f syntax=%.0f%%)",
        best["relevance_threshold"], best["bilingual_sim_threshold"], best["quality_threshold"],
        best["fscore"], best["retention"] * 100, best["avg_score"], best["syntax_rate"] * 100,
    )

    return {
        "project_id":   project_id,
        "adapter":      adapter_name,
        "src_lang":     src_lang,
        "tgt_lang":     tgt_lang,
        "n_samples":    len(sample),
        "n_gen":        n_gen_per_config,
        "best":         best,
        "results":      results_sorted,
        "ts":           datetime.now(timezone.utc).isoformat(),
    }


# ── YAML patch ─────────────────────────────────────────────────────────────────

def _patch_yaml(project_id: str, adapter_name: str, thresholds: dict[str, float]) -> None:
    path = _PROJECTS_DIR / f"{project_id}.yaml"
    text = path.read_text(encoding="utf-8")

    start = text.find("- name: " + adapter_name)
    if start == -1:
        raise ValueError(f"Adapter '{adapter_name}' not found in {path}")

    # Find end of this adapter block (next "- name:" at same indent level)
    next_adapter = text.find("\n- name:", start + 5)
    end = next_adapter if next_adapter != -1 else len(text)

    block = text[start:end]

    # Remove existing threshold lines (idempotent)
    block = re.sub(
        r'\n  (relevance_threshold|bilingual_sim_threshold|quality_threshold):[^\n]*',
        "",
        block,
    )

    # Insert after estimated_pairs line
    insert_lines = (
        "\n  relevance_threshold: " + str(round(thresholds["relevance_threshold"], 2))
        + "\n  bilingual_sim_threshold: " + str(round(thresholds["bilingual_sim_threshold"], 2))
        + "\n  quality_threshold: " + str(round(thresholds["quality_threshold"], 2))
    )

    m = re.search(r"(\n  estimated_pairs:[^\n]*)", block)
    if m:
        insert_at = m.end()
        block = block[:insert_at] + insert_lines + block[insert_at:]
    else:
        # Fallback: append before pair_strategy or at end of block
        m2 = re.search(r"\n  pair_strategy:", block)
        insert_at = m2.start() if m2 else len(block)
        block = block[:insert_at] + insert_lines + block[insert_at:]

    new_text = text[:start] + block + text[end:]
    path.write_text(new_text, encoding="utf-8")
    logger.info("YAML patched: %s → %s", adapter_name, thresholds)


# ── HTML report ────────────────────────────────────────────────────────────────

def _render_html(result: dict[str, Any], output_path: Path) -> None:
    best = result["best"]

    rows = ""
    for r in result["results"]:
        is_best = (
            r["relevance_threshold"]     == best["relevance_threshold"]
            and r["bilingual_sim_threshold"] == best["bilingual_sim_threshold"]
            and r["quality_threshold"]       == best["quality_threshold"]
        )
        tr_style = ' style="background:#e8f5e9"' if is_best else ""
        winner_col = " 🏆" if is_best else ""
        rel_str    = str(r["relevance_threshold"])
        bil_str    = str(r["bilingual_sim_threshold"])
        q_str      = str(r["quality_threshold"])
        ret_str    = str(round(r["retention"] * 100, 1)) + "%"
        avg_str    = str(round(r["avg_score"], 3))
        syn_str    = str(round(r["syntax_rate"] * 100, 1)) + "%"
        fs_str     = str(round(r["fscore"], 4))
        rows += (
            "<tr" + tr_style + ">"
            "<td>" + rel_str + "</td>"
            "<td>" + bil_str + "</td>"
            "<td>" + q_str + "</td>"
            "<td>" + ret_str + "</td>"
            "<td>" + avg_str + "</td>"
            "<td>" + syn_str + "</td>"
            "<td><strong>" + fs_str + winner_col + "</strong></td>"
            "</tr>"
        )

    best_rel = str(best["relevance_threshold"])
    best_bil = str(best["bilingual_sim_threshold"])
    best_q   = str(best["quality_threshold"])
    best_ret = str(round(best["retention"] * 100, 1)) + "%"
    best_avg = str(round(best["avg_score"], 3))
    best_fs  = str(round(best["fscore"], 4))

    html = (
        "<!DOCTYPE html>\n"
        "<html lang='fr'><head><meta charset='utf-8'>\n"
        "<title>Hyperparameter Tune — " + result["adapter"] + "</title>\n"
        "<style>\n"
        "body{font-family:monospace;margin:2em;color:#1a1a2e}\n"
        "table{border-collapse:collapse;width:100%;margin:1em 0}\n"
        "th,td{border:1px solid #ccc;padding:8px;text-align:left}\n"
        "th{background:#003366;color:#fff}\n"
        "h2{color:#003366} .badge{background:#2e7d32;color:#fff;padding:2px 8px;border-radius:3px;font-size:9pt}\n"
        ".warn{background:#fff3e0;border:1px solid #f0d080;padding:8px;border-radius:4px;margin:8px 0}\n"
        "</style></head><body>\n"
        "<h1>Hyperparameter Tuning Report</h1>\n"
        "<p><strong>Adapter:</strong> " + result["adapter"] + " &nbsp;|&nbsp; "
        "<strong>Direction:</strong> " + result["src_lang"] + " -> " + result["tgt_lang"] + " &nbsp;|&nbsp; "
        "<strong>Samples:</strong> " + str(result["n_samples"]) + " &nbsp;|&nbsp; "
        "<strong>Gen/config:</strong> " + str(result["n_gen"]) + " &nbsp;|&nbsp; "
        "<strong>Date:</strong> " + result["ts"][:10] + "</p>\n"
        "<h2>Best configuration <span class='badge'>fscore = " + best_fs + "</span></h2>\n"
        "<table style='width:auto'>\n"
        "<tr><th>Param</th><th>Value</th></tr>\n"
        "<tr><td>relevance_threshold</td><td>" + best_rel + "</td></tr>\n"
        "<tr><td>bilingual_sim_threshold</td><td>" + best_bil + "</td></tr>\n"
        "<tr><td>quality_threshold</td><td>" + best_q + "</td></tr>\n"
        "<tr><td>Retention rate</td><td>" + best_ret + "</td></tr>\n"
        "<tr><td>Avg LLM score</td><td>" + best_avg + "</td></tr>\n"
        "</table>\n"
        "<p class='warn'><strong>Note:</strong> fscore = avg_score × retention_rate. "
        "A high retention with low avg_score may indicate thresholds are too permissive. "
        "Grid: " + str(len(result["results"])) + " combinations tested.</p>\n"
        "<h2>All configurations (sorted by fscore)</h2>\n"
        "<table>\n"
        "<tr><th>rel_thr</th><th>bil_thr</th><th>q_thr</th>"
        "<th>Retention</th><th>Avg score</th><th>Syntax OK</th><th>F-score</th></tr>\n"
        + rows +
        "</table>\n"
        "</body></html>"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    logger.info("Report written to %s", output_path)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tune hyperparameters for generate_bilingual pipeline"
    )
    parser.add_argument("--project",        required=True, help="Project ID")
    parser.add_argument("--adapter",        required=True, help="Adapter name")
    parser.add_argument("--samples",        type=int, default=50,
                        help="Number of clean_raw records to sample (default: 50)")
    parser.add_argument("--gen-per-config", type=int, default=5, dest="gen_per_config",
                        help="Pairs to generate per grid config (default: 5). "
                             "Estimated LLM calls: ~configs × gen × 2")
    parser.add_argument("--seed",           type=int, default=42)
    parser.add_argument("--no-write",       action="store_true", dest="no_write",
                        help="Produce report only — do not patch the YAML")
    args = parser.parse_args()

    result = run_tuner(args.project, args.adapter, args.samples, args.gen_per_config, args.seed)
    best = result["best"]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    report_path = _REPORTS_DIR / (args.adapter + "_" + ts + ".html")
    _render_html(result, report_path)

    if not args.no_write:
        _patch_yaml(args.project, args.adapter, {
            "relevance_threshold":     best["relevance_threshold"],
            "bilingual_sim_threshold": best["bilingual_sim_threshold"],
            "quality_threshold":       best["quality_threshold"],
        })

    sep = "=" * 64
    print("\n" + sep)
    print("Adapter       : " + result["adapter"])
    print("Samples       : " + str(result["n_samples"]))
    print("Gen per config: " + str(result["n_gen"]))
    print(sep)
    print("Best config:")
    print("  relevance_threshold    : " + str(best["relevance_threshold"]))
    print("  bilingual_sim_threshold: " + str(best["bilingual_sim_threshold"]))
    print("  quality_threshold      : " + str(best["quality_threshold"]))
    print("  Retention rate         : " + str(round(best["retention"] * 100, 1)) + "%")
    print("  Avg LLM score          : " + str(round(best["avg_score"], 3)))
    print("  F-score                : " + str(round(best["fscore"], 4)))
    print(sep)
    print("Report : " + str(report_path))
    if not args.no_write:
        print("YAML   : config/projects/" + args.project + ".yaml (patched)")
    else:
        print("YAML   : not modified (--no-write)")
    print(sep)


if __name__ == "__main__":
    main()
