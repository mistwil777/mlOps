"""Extract training pairs from source repositories."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import write_jsonl, count_tokens
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.extract")

_LANG_EXTENSIONS: dict[str, str] = {
    "python": ".py",
    "java": ".java",
    "javascript": ".js",
    "typescript": ".ts",
    "go": ".go",
    "rust": ".rs",
    "cpp": ".cpp",
    "c": ".c",
}

_MISTRAL_TEMPLATE = "[INST] {prompt} [/INST]"
_LLAMA_TEMPLATE = "<|user|>\n{prompt}\n<|assistant|>\n{completion}"


def _load_project_config(project: str) -> dict[str, Any]:
    config_path = Path("config/projects") / f"{project}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Project config not found: {config_path}")
    with config_path.open() as fh:
        return yaml.safe_load(fh)


class PairExtractor:
    def __init__(self, src_lang: str, tgt_lang: str, min_tokens: int = 50, max_tokens: int = 2048):
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self._src_ext = _LANG_EXTENSIONS.get(src_lang, f".{src_lang}")
        self._tgt_ext = _LANG_EXTENSIONS.get(tgt_lang, f".{tgt_lang}")
        self._parser_available = self._check_tree_sitter()

    def _check_tree_sitter(self) -> bool:
        try:
            import tree_sitter_python  # noqa: F401
            import tree_sitter_java  # noqa: F401
            return True
        except ImportError:
            logger.warning("tree-sitter language packs not installed — skipping syntax validation")
            return False

    def validate_syntax(self, code: str, lang: str) -> bool:
        """Return True if code parses without errors."""
        if not self._parser_available:
            return True  # skip validation gracefully

        try:
            from tree_sitter import Language, Parser  # noqa: PLC0415
            import tree_sitter_python as tspy  # noqa: PLC0415
            import tree_sitter_java as tsjava  # noqa: PLC0415

            lang_map = {"python": tspy.language(), "java": tsjava.language()}
            if lang not in lang_map:
                return True

            parser = Parser(Language(lang_map[lang]))
            tree = parser.parse(code.encode())
            return not tree.root_node.has_error
        except Exception as exc:  # noqa: BLE001
            logger.debug("Syntax check failed for %s: %s", lang, exc)
            return False

    def extract_from_repo(self, repo_path: Path | str) -> list[dict[str, Any]]:
        """Walk repo and find matching src/tgt file pairs."""
        repo = Path(repo_path)
        if not repo.exists():
            raise FileNotFoundError(f"Repository not found: {repo}")

        src_files = list(repo.rglob(f"*{self._src_ext}"))
        logger.info("Found %d %s files in %s", len(src_files), self.src_lang, repo)

        pairs: list[dict[str, Any]] = []
        rejected = {"syntax": 0, "too_short": 0, "too_long": 0, "no_match": 0}

        for src_file in src_files:
            # Look for tgt file with same stem in same or sibling directory
            tgt_candidates = [
                src_file.with_suffix(self._tgt_ext),
                src_file.parent.parent / src_file.stem / (src_file.stem + self._tgt_ext),
            ]
            tgt_file = next((p for p in tgt_candidates if p.exists()), None)

            if tgt_file is None:
                rejected["no_match"] += 1
                continue

            src_code = src_file.read_text(encoding="utf-8", errors="replace")
            tgt_code = tgt_file.read_text(encoding="utf-8", errors="replace")

            total_tokens = count_tokens(src_code) + count_tokens(tgt_code)
            if total_tokens < self.min_tokens:
                rejected["too_short"] += 1
                continue
            if total_tokens > self.max_tokens:
                rejected["too_long"] += 1
                continue

            if not self.validate_syntax(src_code, self.src_lang):
                rejected["syntax"] += 1
                continue
            if not self.validate_syntax(tgt_code, self.tgt_lang):
                rejected["syntax"] += 1
                continue

            pairs.append({
                "prompt": src_code,
                "completion": tgt_code,
                "src_file": str(src_file.relative_to(repo)),
                "tgt_file": str(tgt_file.relative_to(repo)),
                "tokens": total_tokens,
            })

        logger.info(
            "Extracted %d pairs. Rejected: syntax=%d too_short=%d too_long=%d no_match=%d",
            len(pairs), rejected["syntax"], rejected["too_short"],
            rejected["too_long"], rejected["no_match"],
        )
        return pairs

    def format_as_jsonl(
        self,
        pairs: list[dict[str, Any]],
        model_format: str = "mistral",
    ) -> list[dict[str, Any]]:
        """Format raw pairs into the prompt/completion format expected by mlx_lm."""
        formatted = []
        for p in pairs:
            if model_format == "mistral":
                prompt = _MISTRAL_TEMPLATE.format(
                    prompt=f"Convert this {self.src_lang} code to {self.tgt_lang}:\n\n{p['prompt']}"
                )
            elif model_format == "llama":
                prompt = f"<|user|>\nConvert this {self.src_lang} code to {self.tgt_lang}:\n\n{p['prompt']}\n<|assistant|>\n"
            else:
                prompt = p["prompt"]

            formatted.append({
                "prompt": prompt,
                "completion": p["completion"],
                "tokens": p["tokens"],
            })
        return formatted


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract training pairs from a repository")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--min-tokens", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--format", default=None)
    args = parser.parse_args()

    config = _load_project_config(args.project)
    src_lang = config["source_lang"]
    tgt_lang = config["target_lang"]
    fmt = args.format or config.get("model", "mistral-7b").split("-")[0]

    extractor = PairExtractor(src_lang, tgt_lang, args.min_tokens, args.max_tokens)
    pairs = extractor.extract_from_repo(args.repo)
    formatted = extractor.format_as_jsonl(pairs, model_format=fmt)

    output = Path(f"datalake/projects/{args.project}/processed/pairs_raw.jsonl")
    write_jsonl(output, formatted)

    print(f"Extracted {len(formatted)} pairs → {output}")


if __name__ == "__main__":
    main()
