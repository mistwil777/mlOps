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

def _load_lang_extensions() -> dict[str, str]:
    """Load lang→extension mapping from config/languages.yaml collector_defaults."""
    path = Path("config/languages.yaml")
    if not path.exists():
        return {}
    with path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg.get("collector_defaults", {}).get("lang_extensions", {})


def _load_task_instructions() -> dict[str, str]:
    """Load task-type instruction templates from config/transforms/*.yaml."""
    transforms_dir = Path("config/transforms")
    instructions: dict[str, str] = {}
    if not transforms_dir.exists():
        return instructions
    for tmpl_path in transforms_dir.glob("*.yaml"):
        with tmpl_path.open() as fh:
            tmpl = yaml.safe_load(fh) or {}
        tmpl_id = tmpl.get("id") or tmpl_path.stem
        if "instruction_template" in tmpl:
            # Strip trailing newline for single-line use
            instructions[tmpl_id] = tmpl["instruction_template"].strip()
    return instructions


# Loaded lazily on first use — avoids import-time file dependency
_LANG_EXTENSIONS_CACHE: dict[str, str] | None = None
_TASK_INSTRUCTIONS_CACHE: dict[str, str] | None = None


def _get_lang_extensions() -> dict[str, str]:
    global _LANG_EXTENSIONS_CACHE
    if _LANG_EXTENSIONS_CACHE is None:
        _LANG_EXTENSIONS_CACHE = _load_lang_extensions()
    return _LANG_EXTENSIONS_CACHE


def _get_task_instructions() -> dict[str, str]:
    global _TASK_INSTRUCTIONS_CACHE
    if _TASK_INSTRUCTIONS_CACHE is None:
        _TASK_INSTRUCTIONS_CACHE = _load_task_instructions()
    return _TASK_INSTRUCTIONS_CACHE


# Module-level dict populated on first access — backward-compatible public alias
LANG_EXTENSIONS: dict[str, str] = {}   # populated lazily by _ensure_loaded()


def _ensure_loaded() -> None:
    global LANG_EXTENSIONS, _LANG_EXTENSIONS_CACHE, _TASK_INSTRUCTIONS_CACHE
    if not LANG_EXTENSIONS:
        LANG_EXTENSIONS.update(_get_lang_extensions())

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


_FALLBACK_INSTRUCTIONS: dict[str, str] = {
    "migration":      "Migrate the following {src_lang} code to {tgt_lang}.",
    "translation":    "Translate the following {src_lang} code to {tgt_lang}.",
    "refactoring":    "Refactor the following {src_lang} code to improve quality.",
    "summarization":  "Summarize the following content in a concise way.",
    "documentation":  "Write documentation for the following code.",
    "review":         "Review the following code and suggest improvements.",
    "extraction":     "Extract the key information from the following content.",
    "classification": "Classify the following content.",
    "qa":             "Answer the following question based on the provided context.",
    "generation":     "Generate the appropriate output for the following input.",
}


def _build_instruction(
    task_type: str,
    domain: str,
    src_lang: str | None = None,
    tgt_lang: str | None = None,
) -> str:
    """Build a domain-aware instruction string for this task type."""
    instructions = _get_task_instructions()
    # Prefer transform-template instructions, fall back to built-in defaults
    template = (instructions.get(task_type)
                or _FALLBACK_INSTRUCTIONS.get(task_type)
                or _FALLBACK_INSTRUCTIONS["generation"])
    return template.format(
        src_lang=src_lang or domain,
        tgt_lang=tgt_lang or domain,
        input="",
        lang=src_lang or domain,
    ).strip()


class PairExtractor:
    """
    Extracts (prompt, completion) pairs from a repo.

    For migration/translation tasks: matches src+tgt files by stem + extension.
    For other task types: treats each file as a standalone sample.

    Optionally enriches each pair with RAG context retrieved from a RagIndex.
    """

    def __init__(
        self,
        domain: str,
        task_type: str,
        src_lang: str | None = None,
        tgt_lang: str | None = None,
        min_tokens: int = 50,
        max_tokens: int = 2048,
        rag_index=None,
    ):
        self.domain = domain
        self.task_type = task_type
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        _ensure_loaded()
        self._src_ext = LANG_EXTENSIONS.get(src_lang or "", f".{src_lang}") if src_lang else None
        self._tgt_ext = LANG_EXTENSIONS.get(tgt_lang or "", f".{tgt_lang}") if tgt_lang else None
        self._rag = rag_index

    def extract_from_repo(self, repo_path: Path | str) -> list[dict[str, Any]]:
        """Walk repo, collect pairs. Strategy depends on task_type."""
        repo = Path(repo_path)
        if not repo.exists():
            raise FileNotFoundError(f"Repository not found: {repo}")

        if self.task_type in ("migration", "translation") and self._src_ext and self._tgt_ext:
            return self._extract_paired(repo)
        else:
            return self._extract_standalone(repo)

    def _extract_paired(self, repo: Path) -> list[dict[str, Any]]:
        """Extract (src, tgt) pairs where files share the same stem."""
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

            rag_context = self._get_rag_context(src_code)

            pairs.append({
                "prompt": src_code,
                "completion": tgt_code,
                "rag_context": rag_context,
                "meta": {
                    "src_file": str(src_file.relative_to(repo)),
                    "tgt_file": str(tgt_file.relative_to(repo)),
                    "tokens": total_tokens,
                    "domain": self.domain,
                    "task_type": self.task_type,
                    "src_lang": self.src_lang,
                    "tgt_lang": self.tgt_lang,
                },
            })

        logger.info(
            "Extracted %d pairs | rejected: no_match=%d too_short=%d too_long=%d",
            len(pairs), rejected["no_match"], rejected["too_short"], rejected["too_long"],
        )
        return pairs

    def _extract_standalone(self, repo: Path) -> list[dict[str, Any]]:
        """Extract individual files as prompt=content, completion=content (self-supervised seed)."""
        ext = self._src_ext or ".txt"
        files = list(repo.rglob(f"*{ext}"))
        logger.info("Found %d %s files for standalone extraction in %s", len(files), ext, repo)

        pairs: list[dict[str, Any]] = []
        rejected = {"too_short": 0, "too_long": 0}

        for f in files:
            content = f.read_text(encoding="utf-8", errors="replace")
            tokens = count_tokens(content)
            if tokens < self.min_tokens:
                rejected["too_short"] += 1
                continue
            if tokens > self.max_tokens:
                rejected["too_long"] += 1
                continue

            rag_context = self._get_rag_context(content)

            pairs.append({
                "prompt": content,
                "completion": content,
                "rag_context": rag_context,
                "meta": {
                    "src_file": str(f.relative_to(repo)),
                    "tokens": tokens,
                    "domain": self.domain,
                    "task_type": self.task_type,
                },
            })

        logger.info(
            "Extracted %d standalone samples | rejected: too_short=%d too_long=%d",
            len(pairs), rejected["too_short"], rejected["too_long"],
        )
        return pairs

    def _get_rag_context(self, text: str) -> str:
        if self._rag is None:
            return ""
        try:
            hits = self._rag.search(text[:512])
            return self._rag.format_context(hits)
        except Exception as exc:
            logger.debug("RAG lookup failed: %s", exc)
            return ""

    def format_as_jsonl(
        self,
        pairs: list[dict[str, Any]],
        model_format: str = "qwen",
    ) -> list[dict[str, Any]]:
        """Wrap pairs in the model chat template. RAG context is prepended when present."""
        template = _TEMPLATES.get(model_format, _TEMPLATES["raw"])
        formatted = []

        instruction_base = _build_instruction(
            self.task_type, self.domain, self.src_lang, self.tgt_lang
        )

        for p in pairs:
            instruction = instruction_base
            if p["rag_context"]:
                instruction += f"\n\n[Reference patterns]\n{p['rag_context']}"

            src_lang_label = self.src_lang or self.domain
            instruction += f"\n\n```{src_lang_label}\n{p['prompt']}\n```"

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
    parser.add_argument("--format",     default=None, help="Override model chat template")
    parser.add_argument("--no-rag",     action="store_true")
    args = parser.parse_args()

    config = _load_project_config(args.project)
    domain    = config.get("domain", "code")
    task_type = config.get("task_type", "migration")
    src_lang  = config.get("src_lang")
    tgt_lang  = config.get("tgt_lang")
    model_fmt = args.format or config.get("model_format", "qwen")

    rag_index = None
    if not args.no_rag and config.get("rag_index"):
        from pipeline.rag_index import build_index  # noqa: PLC0415
        rag_index = build_index(args.project, domain)
        logger.info("RAG index: %d patterns", rag_index.count)

    extractor = PairExtractor(
        domain=domain,
        task_type=task_type,
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        min_tokens=args.min_tokens,
        max_tokens=args.max_tokens,
        rag_index=rag_index,
    )
    pairs     = extractor.extract_from_repo(args.repo)
    formatted = extractor.format_as_jsonl(pairs, model_format=model_fmt)

    output = Path(f"datalake/projects/{args.project}/processed/pairs_raw.jsonl")
    write_jsonl(output, formatted)

    rag_enriched = sum(1 for p in formatted if p["rag_context"])
    print(f"Extracted {len(formatted)} pairs ({rag_enriched} RAG-enriched) → {output}")


if __name__ == "__main__":
    main()
