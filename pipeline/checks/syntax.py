"""Static syntax checker using tree-sitter.

Reads config/languages.yaml collector_defaults.real_langs to know which languages
are supported. Falls back to heuristic brace/indent check for unsupported langs.

CLI:
    python -m pipeline.checks.syntax --input <file.jsonl> [--output <out.jsonl>]
    python -m pipeline.checks.syntax --code "print('hi')" --lang python
"""

from __future__ import annotations

import argparse
import importlib
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.checks.syntax")

_LANG_CONFIG = Path("config/languages.yaml")

# tree-sitter language module names — only for languages that have pip packages
_TS_MODULES: dict[str, str] = {
    "python": "tree_sitter_python",
    "java":   "tree_sitter_java",
}


def _load_supported_langs() -> set[str]:
    try:
        with _LANG_CONFIG.open() as fh:
            cfg = yaml.safe_load(fh) or {}
        return set(cfg.get("collector_defaults", {}).get("real_langs", []))
    except FileNotFoundError:
        return set()


def _ts_parser(lang: str) -> Any | None:
    """Return a tree-sitter Parser for the given language, or None if unavailable."""
    module_name = _TS_MODULES.get(lang.lower())
    if not module_name:
        return None
    try:
        from tree_sitter import Language, Parser  # noqa: PLC0415

        mod = importlib.import_module(module_name)
        language = Language(mod.language())
        parser = Parser(language)
        return parser
    except Exception as exc:  # noqa: BLE001
        logger.debug("tree-sitter parser unavailable for %s: %s", lang, exc)
        return None


def _heuristic_check(code: str, lang: str) -> tuple[bool, str]:
    """Best-effort syntax check when tree-sitter is unavailable for a language."""
    if not code or not code.strip():
        return False, "empty code"
    # Unbalanced braces — catches obvious truncation
    opens  = code.count("{")
    closes = code.count("}")
    if abs(opens - closes) > 2:
        return False, f"unbalanced braces: {opens} {{ vs {closes} }}"
    # Unbalanced parentheses
    opens  = code.count("(")
    closes = code.count(")")
    if abs(opens - closes) > 3:
        return False, f"unbalanced parens: {opens} ( vs {closes} )"
    return True, "heuristic_ok"


def check_syntax(code: str, lang: str) -> tuple[bool, str]:
    """Return (is_valid, reason).

    Uses tree-sitter for python/java; heuristic check for other languages.
    """
    if not code or not code.strip():
        return False, "empty"

    parser = _ts_parser(lang)
    if parser is None:
        return _heuristic_check(code, lang)

    try:
        tree = parser.parse(bytes(code, "utf-8"))
        # tree-sitter marks parse errors with ERROR or MISSING nodes
        has_error = _has_error_node(tree.root_node)
        if has_error:
            return False, "syntax_error"
        return True, "tree_sitter_ok"
    except Exception as exc:  # noqa: BLE001
        logger.debug("tree-sitter parse failed: %s", exc)
        return _heuristic_check(code, lang)


def _has_error_node(node: Any) -> bool:
    if node.type in ("ERROR", "MISSING"):
        return True
    return any(_has_error_node(child) for child in node.children)


def check_record(record: dict[str, Any]) -> tuple[bool, str]:
    """Check syntax of the code field in a raw collector record."""
    code = str(record.get("code", ""))
    lang = str(record.get("lang", "")).lower()
    if not code:
        # Records without a code field (e.g. web_crawl prompt/completion) pass by default
        return True, "no_code_field"
    return check_syntax(code, lang)


def filter_dataset(
    input_path: Path | str,
    output_path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter JSONL file. Return (passed, rejected)."""
    records = read_jsonl(input_path)
    passed:   list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for r in records:
        ok, reason = check_record(r)
        if ok:
            passed.append(r)
        else:
            rejected.append({**r, "_syntax_reject_reason": reason})
            logger.debug(
                "Rejected record (lang=%s): %s — %s",
                r.get("lang"), r.get("task_name") or r.get("description", "")[:60], reason,
            )

    logger.info(
        "Syntax check: %d passed / %d rejected (%.1f%% pass rate)",
        len(passed), len(rejected),
        100 * len(passed) / len(records) if records else 0,
    )

    if output_path and passed:
        write_jsonl(output_path, passed)

    return passed, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="Static syntax check on JSONL records")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", help="JSONL file to check")
    group.add_argument("--code",  help="Single code snippet to check")
    parser.add_argument("--lang",   default="python", help="Language for --code mode")
    parser.add_argument("--output", help="Output JSONL (passed records only)")
    args = parser.parse_args()

    if args.code:
        ok, reason = check_syntax(args.code, args.lang)
        print(f"{'OK' if ok else 'FAIL'}: {reason}")
    else:
        passed, rejected = filter_dataset(args.input, args.output)
        if not args.output:
            print(f"Passed: {len(passed)}  Rejected: {len(rejected)}")


if __name__ == "__main__":
    main()
