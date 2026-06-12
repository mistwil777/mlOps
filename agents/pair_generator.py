"""Pair generator: generate synthetic training pairs for all adapters in one pass.

Used when natural bilingual sources are insufficient to reach training targets.
LLM generates the content — software (AST, regex, ratio) decides acceptance.
No clean_raw needed — generated pairs go directly to processed/pairs/.

Strategies per adapter:
  - pair_strategy=fuse + has Java records  → translate Java→Python via LLM
  - pair_strategy=fuse + no Java           → generate Java+Python pair from adapter description
  - pair_strategy=doc_to_code              → generate (description→code) from adapter context

Design guarantees:
  - Progressive save every SAVE_EVERY pairs — no data loss on crash
  - Max consecutive LLM failures before skip — no infinite loop
  - Per-pair print(flush=True) — real-time visibility in UI logs
  - Bedrock timeout 60s via llm.py config — no silent hang
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.pair_generator")

_RATE_LIMIT_S       = 0.3
_MIN_CODE_CHARS      = 30
_MIN_RATIO           = 0.10
_MAX_RATIO           = 12.0
_SAVE_EVERY          = 10    # save to disk every N pairs
_MAX_CONSEC_FAILURES = 8     # abort adapter after N consecutive LLM failures


# ── Progress printer ──────────────────────────────────────────────────────────

def _progress(adapter: str, done: int, target: int, msg: str = "") -> None:
    pct = int(done / max(target, 1) * 100)
    bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
    line = f"[{adapter[:30]:<30}] {bar} {done:>5}/{target} ({pct}%){' — ' + msg if msg else ''}"
    print(line, flush=True)


# ── Software verifiers ────────────────────────────────────────────────────────

def _verify_python(code: str) -> tuple[bool, str]:
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    try:
        ast.parse(code)
        return True, "ok"
    except SyntaxError as e:
        return False, f"syntax_error: {e}"


def _verify_java(code: str) -> tuple[bool, str]:
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    java_signals = re.compile(
        r'\b(class|interface|public|private|protected|void|int|String|return|import)\b'
    )
    if not java_signals.search(code):
        return False, "no_java_keywords"
    if code.count("{") != code.count("}"):
        return False, "unbalanced_braces"
    return True, "ok"


def _verify_ratio(prompt: str, completion: str) -> tuple[bool, str]:
    if not prompt:
        return False, "empty_prompt"
    ratio = len(completion) / max(len(prompt), 1)
    if ratio < _MIN_RATIO:
        return False, f"too_short (ratio={ratio:.2f})"
    if ratio > _MAX_RATIO:
        return False, f"too_long (ratio={ratio:.2f})"
    return True, "ok"


def _extract_code(text: str, lang: str) -> str:
    m = re.search(rf"```{re.escape(lang)}\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m: return m.group(1).strip()
    m = re.search(r"```\w*\n(.*?)```", text, re.DOTALL)
    if m: return m.group(1).strip()
    return text.strip()


# ── Prompt templates ──────────────────────────────────────────────────────────

_TRANSLATE_PROMPT = """\
You are an expert code translator. Translate the following {src_lang} code to idiomatic {tgt_lang}.

Rules:
- Output ONLY the {tgt_lang} code, no explanations
- Preserve the exact logic and structure
- Use {tgt_lang} idioms and best practices

{src_lang} code:
{source_code}
"""

_GENERATE_PAIR_PROMPT = """\
You are a {tgt_lang} and {src_lang} expert. Generate a realistic training pair for a model that \
translates {src_lang} to {tgt_lang} in the context of: {adapter_context}

Generate:
1. A {src_lang} code snippet (10-30 lines, realistic, typical of {adapter_context})
2. Its idiomatic {tgt_lang} equivalent

Output format (EXACTLY):
```{src_lang}
<source code here>
```
```{tgt_lang}
<target code here>
```
"""

_DOC_TO_CODE_PROMPT = """\
You are a {lang} expert in {adapter_context}. \
Generate a realistic description + code pair for training a code generation model.

Output format (EXACTLY):
DESCRIPTION: <one sentence describing what the code does>
```{lang}
<10-30 lines of realistic {lang} code>
```
"""


# ── Per-adapter generator ─────────────────────────────────────────────────────

class AdapterPairGenerator:

    def __init__(
        self,
        adapter: dict[str, Any],
        project_id: str,
        existing_pairs: list[dict[str, Any]],
        target: int,
        output_file: Path,
    ):
        self.adapter      = adapter
        self.project_id   = project_id
        self.name         = adapter["name"]
        self.strategy     = adapter.get("pair_strategy", "doc_to_code")
        self.lang_pair    = adapter.get("lang_pair")
        self.description  = adapter.get("description", self.name)
        self.existing     = existing_pairs
        self.target       = target
        self.needed       = max(0, target - len(existing_pairs))
        self.output_file  = output_file

        if self.lang_pair:
            parts = self.lang_pair.split("-")
            self.src_lang = parts[0] if len(parts) >= 1 else "java"
            self.tgt_lang = parts[1] if len(parts) >= 2 else "python"
        else:
            self.src_lang = "python"
            self.tgt_lang = "python"

        self._java_records: list[dict[str, Any]] | None = None
        # Running buffer — flushed to disk every SAVE_EVERY pairs
        self._buffer: list[dict[str, Any]] = list(existing_pairs)

    def _load_java_records(self) -> list[dict[str, Any]]:
        if self._java_records is not None:
            return self._java_records
        cr_file = Path(f"datalake/projects/{self.project_id}/processed/clean_raw/{self.name}.jsonl")
        if not cr_file.exists():
            self._java_records = []
            return []
        all_recs = read_jsonl(cr_file)
        self._java_records = [
            r for r in all_recs
            if r.get("lang", "").lower() == self.src_lang.lower()
            and r.get("code") and len(r.get("code", "")) >= _MIN_CODE_CHARS
        ]
        return self._java_records

    def _save(self) -> None:
        """Progressive save — write current buffer to disk."""
        write_jsonl(self.output_file, self._buffer)

    def _accept(self, pair: dict[str, Any]) -> None:
        """Accept a pair, add to buffer, save every SAVE_EVERY."""
        self._buffer.append(pair)
        done = len(self._buffer) - len(self.existing)
        if done % _SAVE_EVERY == 0:
            self._save()
        _progress(self.name, done, self.needed)

    def generate(self, dry_run: bool = False) -> int:
        if self.needed <= 0:
            print(f"[{self.name[:30]:<30}] Déjà à la cible ({len(self.existing)}/{self.target}) — ignoré", flush=True)
            return 0

        print(f"\n{'='*60}", flush=True)
        print(f"Adapter : {self.name}", flush=True)
        print(f"Stratégie : {self.strategy} | {self.src_lang}→{self.tgt_lang}", flush=True)
        print(f"À générer : {self.needed} paires (existantes : {len(self.existing)}, cible : {self.target})", flush=True)
        print(f"{'='*60}", flush=True)

        if dry_run:
            print(f"[DRY RUN] Would generate {self.needed} pairs for {self.name}", flush=True)
            return 0

        if self.strategy == "fuse":
            java_recs = self._load_java_records()
            if java_recs:
                print(f"Mode : traduction ({len(java_recs)} records Java disponibles)", flush=True)
                n = self._generate_by_translation(java_recs)
            else:
                print("Mode : génération synthétique (pas de Java dans clean_raw)", flush=True)
                n = self._generate_synthetic_pairs()
        else:
            print(f"Mode : doc_to_code ({self.tgt_lang})", flush=True)
            n = self._generate_doc_to_code()

        self._save()  # final save
        print(f"\n✓ {self.name} terminé : {n} nouvelles paires générées", flush=True)
        return n

    def _generate_by_translation(self, java_recs: list[dict[str, Any]]) -> int:
        done = 0
        rejected = {"syntax": 0, "ratio": 0, "llm": 0}
        consec_fail = 0
        idx = 0

        while done < self.needed:
            rec = java_recs[idx % len(java_recs)]
            idx += 1
            source_code = rec.get("code", "")

            prompt = _TRANSLATE_PROMPT.format(
                src_lang=self.src_lang,
                tgt_lang=self.tgt_lang,
                source_code=source_code[:1500],
            )
            response = call_llm(prompt, max_tokens=1024, temperature=0.2)
            if not response:
                rejected["llm"] += 1
                consec_fail += 1
                if consec_fail >= _MAX_CONSEC_FAILURES:
                    print(f"⚠ {_MAX_CONSEC_FAILURES} échecs LLM consécutifs — adapter ignoré", flush=True)
                    break
                time.sleep(_RATE_LIMIT_S)
                continue

            consec_fail = 0
            generated = _extract_code(response, self.tgt_lang)

            ok, _ = _verify_ratio(source_code, generated)
            if not ok:
                rejected["ratio"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            ok, _ = _verify_python(generated) if self.tgt_lang == "python" else _verify_java(generated)
            if not ok:
                rejected["syntax"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            self._accept(self._make_pair(
                prompt=source_code, completion=generated,
                gen_type="translated", source_url=rec.get("source_url"),
            ))
            done += 1
            time.sleep(_RATE_LIMIT_S)

        print(f"Rejetés — syntax:{rejected['syntax']} ratio:{rejected['ratio']} llm:{rejected['llm']}", flush=True)
        return done

    def _generate_synthetic_pairs(self) -> int:
        done = 0
        rejected = {"syntax": 0, "ratio": 0, "llm": 0, "parse": 0}
        consec_fail = 0
        max_attempts = self.needed * 5

        for attempt in range(max_attempts):
            if done >= self.needed:
                break

            prompt = _GENERATE_PAIR_PROMPT.format(
                src_lang=self.src_lang,
                tgt_lang=self.tgt_lang,
                adapter_context=self.description[:300],
            )
            response = call_llm(prompt, max_tokens=1536, temperature=0.4)
            if not response:
                rejected["llm"] += 1
                consec_fail += 1
                if consec_fail >= _MAX_CONSEC_FAILURES:
                    print(f"⚠ {_MAX_CONSEC_FAILURES} échecs LLM consécutifs — adapter ignoré", flush=True)
                    break
                time.sleep(_RATE_LIMIT_S)
                continue

            consec_fail = 0
            src_code = _extract_code(response, self.src_lang)
            tgt_code = _extract_code(response, self.tgt_lang)

            if not src_code or not tgt_code or src_code == tgt_code:
                rejected["parse"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            ok, _ = _verify_ratio(src_code, tgt_code)
            if not ok:
                rejected["ratio"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            ok, _ = _verify_python(tgt_code) if self.tgt_lang == "python" else _verify_java(tgt_code)
            if not ok:
                rejected["syntax"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            self._accept(self._make_pair(prompt=src_code, completion=tgt_code, gen_type="synthetic"))
            done += 1
            time.sleep(_RATE_LIMIT_S)

        print(f"Rejetés — syntax:{rejected['syntax']} ratio:{rejected['ratio']} llm:{rejected['llm']} parse:{rejected['parse']}", flush=True)
        return done

    def _generate_doc_to_code(self) -> int:
        done = 0
        rejected = {"syntax": 0, "ratio": 0, "llm": 0, "parse": 0}
        consec_fail = 0
        max_attempts = self.needed * 4

        for attempt in range(max_attempts):
            if done >= self.needed:
                break

            prompt = _DOC_TO_CODE_PROMPT.format(
                lang=self.tgt_lang,
                adapter_context=self.description[:300],
            )
            response = call_llm(prompt, max_tokens=1024, temperature=0.4)
            if not response:
                rejected["llm"] += 1
                consec_fail += 1
                if consec_fail >= _MAX_CONSEC_FAILURES:
                    print(f"⚠ {_MAX_CONSEC_FAILURES} échecs LLM consécutifs — adapter ignoré", flush=True)
                    break
                time.sleep(_RATE_LIMIT_S)
                continue

            consec_fail = 0
            desc_m = re.search(r"DESCRIPTION:\s*(.+?)(?:\n|```)", response, re.DOTALL)
            code = _extract_code(response, self.tgt_lang)

            if not desc_m or not code:
                rejected["parse"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            description = desc_m.group(1).strip()[:300]
            instruction = f"Write {self.tgt_lang} code that does the following:\n\n{description}"

            ok, _ = _verify_ratio(instruction, code)
            if not ok:
                rejected["ratio"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            ok, _ = _verify_python(code) if self.tgt_lang == "python" else (True, "ok")
            if not ok:
                rejected["syntax"] += 1
                time.sleep(_RATE_LIMIT_S)
                continue

            self._accept(self._make_pair(prompt=instruction, completion=code, gen_type="doc_to_code"))
            done += 1
            time.sleep(_RATE_LIMIT_S)

        print(f"Rejetés — syntax:{rejected['syntax']} ratio:{rejected['ratio']} llm:{rejected['llm']} parse:{rejected['parse']}", flush=True)
        return done

    def _make_pair(
        self, prompt: str, completion: str,
        gen_type: str, source_url: str | None = None,
    ) -> dict[str, Any]:
        return {
            "prompt":         prompt,
            "completion":     completion,
            "adapter_name":   self.name,
            "source_lang":    self.src_lang,
            "target_lang":    self.tgt_lang,
            "collector_type": f"generated_{gen_type}",
            "generated":      True,
            "collected_at":   datetime.now(timezone.utc).isoformat(),
            **({"source_url": source_url} if source_url else {}),
        }


# ── Project-level generator ───────────────────────────────────────────────────

def generate_all(project_id: str, dry_run: bool = False) -> dict[str, int]:
    cfg_path = Path("config/projects") / f"{project_id}.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Project config not found: {cfg_path}")
    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)

    pairs_dir = Path(f"datalake/projects/{project_id}/processed/pairs")
    pairs_dir.mkdir(parents=True, exist_ok=True)

    total_needed = sum(
        max(0, a.get("estimated_pairs", 500) - sum(1 for _ in (pairs_dir / f"{a['name']}.jsonl").open() if _.strip()) if (pairs_dir / f"{a['name']}.jsonl").exists() else a.get("estimated_pairs", 500))
        for a in cfg.get("adapters", [])
    )
    print(f"\n{'#'*60}", flush=True)
    print(f"# GÉNÉRATION — projet : {project_id}", flush=True)
    print(f"# Total à générer : {total_needed} paires", flush=True)
    print(f"# Mode : {'DRY RUN' if dry_run else 'PRODUCTION (appels Bedrock actifs)'}", flush=True)
    print(f"{'#'*60}\n", flush=True)

    results: dict[str, int] = {}

    for adapter in cfg.get("adapters", []):
        name   = adapter["name"]
        target = adapter.get("estimated_pairs", 500)
        out_file = pairs_dir / f"{name}.jsonl"
        existing = read_jsonl(out_file) if out_file.exists() else []

        if len(existing) >= target:
            print(f"[{name[:30]:<30}] Déjà complet ({len(existing)}/{target}) — ignoré", flush=True)
            results[name] = 0
            continue

        gen = AdapterPairGenerator(
            adapter=adapter, project_id=project_id,
            existing_pairs=existing, target=target,
            output_file=out_file,
        )
        n = gen.generate(dry_run=dry_run)
        results[name] = n

    total_generated = sum(results.values())
    print(f"\n{'#'*60}", flush=True)
    print(f"# GÉNÉRATION TERMINÉE", flush=True)
    print(f"# Total nouvelles paires : {total_generated}", flush=True)
    for adapter_name, n in results.items():
        if n > 0:
            print(f"#   {adapter_name}: +{n}", flush=True)
    print(f"{'#'*60}", flush=True)

    return results


def estimate_cost(project_id: str) -> dict[str, Any]:
    cfg_path = Path("config/projects") / f"{project_id}.yaml"
    with cfg_path.open() as fh:
        cfg = yaml.safe_load(fh)

    pairs_dir = Path(f"datalake/projects/{project_id}/processed/pairs")
    total_needed = 0
    per_adapter: list[dict[str, Any]] = []

    for adapter in cfg.get("adapters", []):
        name   = adapter["name"]
        target = adapter.get("estimated_pairs", 500)
        out    = pairs_dir / f"{name}.jsonl"
        existing = sum(1 for _ in out.open() if _.strip()) if out.exists() else 0
        needed = max(0, target - existing)
        total_needed += needed
        per_adapter.append({"adapter": name, "existing": existing, "target": target, "needed": needed})

    cost_usd = round(total_needed * 0.0038, 2)
    cost_eur  = round(cost_usd * 0.92, 2)

    return {
        "total_needed": total_needed,
        "cost_usd": cost_usd,
        "cost_eur": cost_eur,
        "per_adapter": per_adapter,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project",  required=True)
    parser.add_argument("--dry-run",  action="store_true", dest="dry_run")
    parser.add_argument("--estimate", action="store_true")
    args = parser.parse_args()

    if args.estimate or args.dry_run:
        est = estimate_cost(args.project)
        print(f"\n── Estimation génération ────────────────────")
        print(f"  Total paires à générer : {est['total_needed']}")
        print(f"  Coût estimé            : ${est['cost_usd']} USD (~{est['cost_eur']} EUR)")
        print()
        for a in est["per_adapter"]:
            if a["needed"] > 0:
                print(f"  {a['adapter'][:45]:<45} {a['existing']:>5}/{a['target']:>5}  → +{a['needed']}")
        if args.estimate:
            return

    sys.stdout.reconfigure(line_buffering=True)  # ensure flush on every line
    generate_all(args.project, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
