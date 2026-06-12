"""Pair fuser: generate target-lang code from source-lang records via LLM, verified by software.

Workflow:
  1. Load source-lang records from processed/clean_raw/<adapter>.jsonl
  2. For each record: call LLM to translate source code to target language
  3. Verify generated code with software (AST for Python, regex for Java structure)
  4. Verify length ratio (completion must be proportional to source)
  5. Write verified pairs to processed/pairs/<adapter>.jsonl

LLM is used ONLY for generation. All acceptance/rejection decisions are made by software.
Generic: driven entirely by config/languages.yaml — no hardcoded language names.
"""

from __future__ import annotations

import argparse
import ast
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.llm import call_llm
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.pair_fuser")

_RATE_LIMIT_S      = 0.2   # seconds between LLM calls
_MIN_CODE_CHARS    = 30    # minimum chars for generated code to be valid
_MAX_RATIO         = 8.0   # max ratio len(completion) / len(source) — reject aberrant outputs
_MIN_RATIO         = 0.15  # min ratio — reject near-empty completions


def _load_languages() -> dict[str, Any]:
    path = Path("config/languages.yaml")
    if not path.exists():
        raise FileNotFoundError("config/languages.yaml not found")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _source_code(r: dict[str, Any]) -> str:
    return (r.get("code") or r.get("completion") or r.get("prompt") or "").strip()


def _description(r: dict[str, Any]) -> str:
    return (r.get("description") or r.get("task_name") or "").strip()


def _extract_code_block(text: str, lang: str) -> str:
    """Extract first fenced code block from LLM response, or return raw text."""
    m = re.search(rf"```{re.escape(lang)}\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\w*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # No fence — return raw response stripped
    return text.strip()


def _verify_python(code: str) -> tuple[bool, str]:
    """Verify Python code syntactically via AST. Returns (ok, reason)."""
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    try:
        ast.parse(code)
        return True, "ok"
    except SyntaxError as e:
        return False, f"syntax_error: {e}"


def _verify_java(code: str) -> tuple[bool, str]:
    """Verify Java code structurally via regex heuristics (no compiler available)."""
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    # Must contain at least one Java structural keyword
    java_signals = re.compile(
        r'\b(class|interface|public|private|protected|void|int|String|return|import)\b'
    )
    if not java_signals.search(code):
        return False, "no_java_keywords"
    # Must have balanced braces
    if code.count("{") != code.count("}"):
        return False, "unbalanced_braces"
    return True, "ok"


_VERIFIERS: dict[str, Any] = {
    "python": _verify_python,
    "java":   _verify_java,
}


def _verify_code(code: str, lang: str) -> tuple[bool, str]:
    verifier = _VERIFIERS.get(lang.lower())
    if verifier:
        return verifier(code)
    # Generic: just check length and non-empty
    if len(code) < _MIN_CODE_CHARS:
        return False, f"too_short ({len(code)} chars)"
    return True, "ok"


def _verify_ratio(source: str, generated: str) -> tuple[bool, str]:
    """Reject wildly disproportionate pairs."""
    if not source:
        return False, "empty_source"
    ratio = len(generated) / max(len(source), 1)
    if ratio < _MIN_RATIO:
        return False, f"completion_too_short (ratio={ratio:.2f})"
    if ratio > _MAX_RATIO:
        return False, f"completion_too_long (ratio={ratio:.2f})"
    return True, "ok"


_TRANSLATION_PROMPT = """\
You are a code translation expert. Translate the following {source_lang} code to idiomatic {target_lang}.

Rules:
- Output ONLY the translated {target_lang} code, no explanations, no markdown
- Preserve the logic and structure exactly
- Use {target_lang} idioms and conventions
- Do not add comments unless they were in the original

{source_lang} code:
{source_code}
"""


class PairFuser:
    """
    Generate target-lang code from source-lang records via LLM.
    All acceptance decisions are made by software (AST, regex, ratio checks).
    """

    def __init__(
        self,
        source_lang: str,
        target_lang: str,
        max_tokens: int = 2048,
    ):
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.max_tokens  = max_tokens

    def fuse(
        self,
        source_records: list[dict[str, Any]],
        max_fused: int = 500,
    ) -> list[dict[str, Any]]:
        records = [r for r in source_records if _source_code(r)]
        if not records:
            logger.warning("No records with code for lang=%s", self.source_lang)
            return []

        logger.info(
            "Generating %s→%s translation for %d records (max=%d)…",
            self.source_lang, self.target_lang, len(records), max_fused
        )

        fused: list[dict[str, Any]] = []
        rejected = {"syntax": 0, "ratio": 0, "llm_fail": 0}

        for i, rec in enumerate(records):
            if len(fused) >= max_fused:
                break

            source_code = _source_code(rec)
            prompt = _TRANSLATION_PROMPT.format(
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                source_code=source_code[:1500],
            )

            response = call_llm(prompt, max_tokens=self.max_tokens, temperature=0.1)
            if not response:
                rejected["llm_fail"] += 1
                logger.debug("[%d/%d] LLM failed — skipped", i + 1, len(records))
                time.sleep(_RATE_LIMIT_S)
                continue

            generated = _extract_code_block(response, self.target_lang)

            # Software verification — ratio check
            ok, reason = _verify_ratio(source_code, generated)
            if not ok:
                rejected["ratio"] += 1
                logger.debug("[%d/%d] rejected (ratio): %s", i + 1, len(records), reason)
                time.sleep(_RATE_LIMIT_S)
                continue

            # Software verification — language-specific syntax/structure check
            ok, reason = _verify_code(generated, self.target_lang)
            if not ok:
                rejected["syntax"] += 1
                logger.debug("[%d/%d] rejected (syntax): %s", i + 1, len(records), reason)
                time.sleep(_RATE_LIMIT_S)
                continue

            fused.append({
                "prompt":         source_code,
                "completion":     generated,
                "source_lang":    self.source_lang,
                "target_lang":    self.target_lang,
                "description":    _description(rec),
                "source_url":     rec.get("source_url"),
                "adapter_name":   rec.get("adapter_name"),
                "collector_type": "fused",
                "collected_at":   datetime.now(timezone.utc).isoformat(),
            })

            logger.info(
                "[%d/%d] ✓ generated %d chars %s → verified",
                len(fused), min(len(records), max_fused),
                len(generated), self.target_lang,
            )
            time.sleep(_RATE_LIMIT_S)

        logger.info(
            "Fusion complete: %d pairs generated | rejected: syntax=%d ratio=%d llm_fail=%d",
            len(fused), rejected["syntax"], rejected["ratio"], rejected["llm_fail"],
        )
        return fused

    def run(
        self,
        source_jsonl: Path,
        output: Path,
        adapter_name: str | None = None,
        max_fused: int = 500,
    ) -> int:
        source_records = read_jsonl(source_jsonl) if source_jsonl.exists() else []
        source_records = [
            r for r in source_records
            if r.get("lang", "").lower() == self.source_lang.lower()
        ]

        logger.info(
            "Loaded %d %s records from %s",
            len(source_records), self.source_lang, source_jsonl,
        )

        fused = self.fuse(source_records, max_fused=max_fused)

        if adapter_name:
            for p in fused:
                p["adapter_name"] = adapter_name

        if fused:
            output.parent.mkdir(parents=True, exist_ok=True)
            existing = read_jsonl(output) if output.exists() else []
            seen = {(r.get("prompt", ""), r.get("completion", "")) for r in existing}
            new_unique = [p for p in fused if (p["prompt"], p["completion"]) not in seen]
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d fused pairs → %s", len(new_unique), output)

        return len(fused)


def main() -> None:
    cfg = _load_languages()
    pairs_cfg = cfg.get("pairs", {})

    parser = argparse.ArgumentParser(
        description="Generate target-lang code from source-lang records via LLM + software verification")
    parser.add_argument("--pair",         required=True, choices=list(pairs_cfg.keys()),
                        help="Language pair key from config/languages.yaml")
    parser.add_argument("--source-jsonl", required=True, dest="source_jsonl")
    parser.add_argument("--output",       required=True)
    parser.add_argument("--max-fused",    type=int, default=500, dest="max_fused")
    parser.add_argument("--max-tokens",   type=int, default=2048, dest="max_tokens")
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    args = parser.parse_args()

    pcfg = pairs_cfg[args.pair]
    fuser = PairFuser(
        source_lang=pcfg["source_lang"],
        target_lang=pcfg["target_lang"],
        max_tokens=args.max_tokens,
    )
    n = fuser.run(
        source_jsonl=Path(args.source_jsonl),
        output=Path(args.output),
        adapter_name=args.adapter_name,
        max_fused=args.max_fused,
    )
    print(f"Fused {n} pairs → {args.output}")


if __name__ == "__main__":
    main()
