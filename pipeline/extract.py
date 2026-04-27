"""Extract training pairs from source repositories."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import write_jsonl, count_tokens
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.extract")

LANG_EXTENSIONS: dict[str, str] = {
    "python":     ".py",
    "java":       ".java",
    "javascript": ".js",
    "typescript": ".ts",
    "go":         ".go",
    "rust":       ".rs",
    "csharp":     ".cs",
    "cpp":        ".cpp",
    "php":        ".php",
    "kotlin":     ".kt",
    "swift":      ".swift",
    "ruby":       ".rb",
    "c":          ".c",
}

# Chat templates — prompt placeholder is {prompt}
_TEMPLATES: dict[str, str] = {
    "mistral": "[INST] {prompt} [/INST]",
    "qwen":    "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n",
    "llama":   "<|user|>\n{prompt}\n<|assistant|>\n",
    "raw":     "{prompt}",
}


def _load_project_config(project: str) -> dict[str, Any]:
    config_path = Path("config/projects") / f"{project}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Project config not found: {config_path}")
    with config_path.open() as fh:
        return yaml.safe_load(fh)


class PairExtractor:
    """
    Extracts (src_code, tgt_code) pairs from a repo where src and tgt files
    share the same stem and differ only by extension.

    Optionally enriches each pair with RAG context retrieved from a RagIndex.
    The RAG context is injected into the prompt at format time so the model
    learns both RAG-guided and standalone modes.
    """

    def __init__(
        self,
        src_lang: str,
        tgt_lang: str,
        min_tokens: int = 50,
        max_tokens: int = 2048,
        rag_index=None,
    ):
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self._src_ext = LANG_EXTENSIONS.get(src_lang, f".{src_lang}")
        self._tgt_ext = LANG_EXTENSIONS.get(tgt_lang, f".{tgt_lang}")
        self._rag = rag_index

    def extract_from_repo(self, repo_path: Path | str) -> list[dict[str, Any]]:
        """Walk repo, collect pairs where src+tgt share the same stem."""
        repo = Path(repo_path)
        if not repo.exists():
            raise FileNotFoundError(f"Repository not found: {repo}")

        src_files = list(repo.rglob(f"*{self._src_ext}"))
        logger.info("Found %d %s files in %s", len(src_files), self.src_lang, repo)

        pairs: list[dict[str, Any]] = []
        rejected = {"too_short": 0, "too_long": 0, "no_match": 0}

        for src_file in src_files:
            tgt_file = src_file.with_suffix(self._tgt_ext)
            if not tgt_file.exists():
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

            rag_context = ""
            if self._rag is not None:
                try:
                    hits = self._rag.search(src_code[:512])
                    rag_context = self._rag.format_context(hits)
                except Exception as exc:
                    logger.debug("RAG lookup failed for %s: %s", src_file.name, exc)

            pairs.append({
                "prompt": src_code,
                "completion": tgt_code,
                "rag_context": rag_context,
                "meta": {
                    "src_file": str(src_file.relative_to(repo)),
                    "tgt_file": str(tgt_file.relative_to(repo)),
                    "tokens": total_tokens,
                    "src_lang": self.src_lang,
                    "tgt_lang": self.tgt_lang,
                },
            })

        logger.info(
            "Extracted %d pairs | rejected: no_match=%d too_short=%d too_long=%d",
            len(pairs), rejected["no_match"], rejected["too_short"], rejected["too_long"],
        )
        return pairs

    def format_as_jsonl(
        self,
        pairs: list[dict[str, Any]],
        model_format: str = "qwen",
    ) -> list[dict[str, Any]]:
        """Wrap pairs in the model chat template. RAG context is prepended when present."""
        template = _TEMPLATES.get(model_format, _TEMPLATES["raw"])
        formatted = []

        for p in pairs:
            instruction = f"Convert the following {self.src_lang} code to {self.tgt_lang}."
            if p["rag_context"]:
                instruction += f"\n\n[Reference patterns]\n{p['rag_context']}"
            instruction += f"\n\n```{self.src_lang}\n{p['prompt']}\n```"

            formatted.append({
                "prompt": template.format(prompt=instruction),
                "completion": p["completion"],
                "rag_context": p["rag_context"],
                "meta": p["meta"],
            })

        return formatted


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract training pairs from a repository")
    parser.add_argument("--repo",       required=True)
    parser.add_argument("--project",    required=True)
    parser.add_argument("--min-tokens", type=int, default=50)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--format",     default=None,  help="Override model chat template")
    parser.add_argument("--no-rag",     action="store_true")
    parser.add_argument("--no-ollama",  action="store_true", help="Dummy embeddings (dev/CI)")
    args = parser.parse_args()

    config = _load_project_config(args.project)
    src_lang = config["source_lang"]
    tgt_lang = config["target_lang"]
    model_fmt = args.format or config.get("model_format", "qwen")

    rag_index = None
    if not args.no_rag and config.get("rag_index"):
        from pipeline.rag_index import build_index
        rag_index = build_index(
            args.project, src_lang, tgt_lang,
            use_ollama=not args.no_ollama,
        )
        logger.info("RAG index: %d patterns", rag_index.count)

    extractor = PairExtractor(src_lang, tgt_lang, args.min_tokens, args.max_tokens, rag_index=rag_index)
    pairs     = extractor.extract_from_repo(args.repo)
    formatted = extractor.format_as_jsonl(pairs, model_format=model_fmt)

    output = Path(f"datalake/projects/{args.project}/processed/pairs_raw.jsonl")
    write_jsonl(output, formatted)

    rag_enriched = sum(1 for p in formatted if p["rag_context"])
    print(f"Extracted {len(formatted)} pairs ({rag_enriched} RAG-enriched) → {output}")


if __name__ == "__main__":
    main()
