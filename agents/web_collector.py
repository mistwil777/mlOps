"""Web collector agent: crawl domain sources and extract neutral records.

Output schema (neutral — no prompt/completion for code domains):
  code domain  → {description, code, lang, source_url, collected_at, adapter_name?}
  text domain  → {prompt, completion, source_url, domain, task_type, collected_at, adapter_name?}

The transformation into training pairs is handled downstream by pipeline/transform.py.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.jsonl import write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.web_collector")

_SOURCES_DIR      = Path("config/sources")
_RATE_LIMIT_S     = 1.0
_MAX_BLOCK_CHARS  = 10_000
_MIN_BLOCK_CHARS  = 100


def _load_source_registry(domain: str) -> dict[str, Any]:
    """Load config/sources/<domain>.yaml. Returns empty dict if not found."""
    path = _SOURCES_DIR / f"{domain}.yaml"
    if not path.exists():
        logger.warning("No source registry found for domain '%s' at %s", domain, path)
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _resolve_urls(domain: str, topic: str) -> list[tuple[str, str]]:
    """
    Resolve ordered URL list for (domain, topic).
    Returns list of (url, source_tier) — tier = official_docs | official_repo |
    quality_repos | github_search.
    If topic is a URL, use it directly without consulting the registry.
    """
    # Direct URL — scrape it immediately, skip registry lookup
    if topic.startswith(("http://", "https://")):
        return [(topic, "direct_url")]

    registry = _load_source_registry(domain)
    topics: dict[str, Any] = registry.get("topics", {})

    # Try exact topic match first, then fall back to first available topic
    entry = topics.get(topic) or (next(iter(topics.values())) if topics else {})
    if not entry:
        logger.warning("No sources found for domain='%s' topic='%s' — falling back to GitHub search", domain, topic)
        return [(f"https://github.com/search?q={topic.replace(' ', '+')}&type=code", "github_search")]

    tiers = ["official_docs", "official_repo", "quality_repos", "github_search"]
    urls: list[tuple[str, str]] = []
    for tier in tiers:
        for item in entry.get(tier, []):
            if tier == "github_search":
                encoded = item.replace(" ", "+")
                urls.append((f"https://github.com/search?q={encoded}&type=code", tier))
            else:
                urls.append((item, tier))
    return urls


def _check_crawl4ai() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except ImportError:
        logger.warning("crawl4ai not installed — run: pip install crawl4ai")
        return False


_CRAWL_TIMEOUT_S = 30  # hard cap per URL — prevents subprocess hang


async def _crawl_url(url: str) -> str | None:
    """Crawl a single URL and return markdown content."""
    try:
        from crawl4ai import AsyncWebCrawler  # noqa: PLC0415
        from crawl4ai.async_configs import CrawlerRunConfig  # noqa: PLC0415

        config = CrawlerRunConfig(page_timeout=_CRAWL_TIMEOUT_S * 1000)
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=config),
                timeout=_CRAWL_TIMEOUT_S,
            )
            return result.markdown if result.success else None
    except asyncio.TimeoutError:
        logger.warning("Crawl timed out after %ds for %s", _CRAWL_TIMEOUT_S, url)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Crawl failed for %s: %s", url, exc)
        return None


def _extract_text_blocks(markdown: str) -> list[str]:
    """Extract meaningful blocks: fenced code blocks + substantial paragraphs."""
    import re  # noqa: PLC0415
    blocks: list[str] = []

    # Fenced code blocks (any language)
    for m in re.finditer(r"```[^\n]*\n(.*?)```", markdown, re.DOTALL):
        b = m.group(1).strip()
        if _MIN_BLOCK_CHARS <= len(b) <= _MAX_BLOCK_CHARS:
            blocks.append(b)

    # Substantial paragraphs (docs, legal texts, etc.)
    for para in re.split(r"\n{2,}", markdown):
        p = para.strip()
        if _MIN_BLOCK_CHARS <= len(p) <= _MAX_BLOCK_CHARS and not p.startswith("#"):
            blocks.append(p)

    return blocks


def _build_records(
    blocks: list[str],
    url: str,
    domain: str,
    task_type: str,
    lang: str | None = None,
) -> list[dict[str, Any]]:
    """
    Build neutral records from extracted blocks.

    Code domains → neutral schema {description, code, lang, source_url, collected_at}
      so that transform.py can form pairs later (doc_to_code or semantic_pair_match).

    Text domains → legacy prompt/completion schema for QA use cases.
    """
    now  = datetime.now(timezone.utc).isoformat()
    recs: list[dict[str, Any]] = []

    if domain == "code":
        # Alternate: description block (text) then code block
        import re as _re  # noqa: PLC0415
        for i in range(0, len(blocks) - 1, 2):
            desc_block = blocks[i].strip()
            code_block = blocks[i + 1].strip()
            # If first block looks like code, swap
            if _re.search(r'\b(def |public |import |class )\b', desc_block):
                desc_block, code_block = code_block, desc_block
            if desc_block and code_block:
                recs.append({
                    "description":  desc_block[:500],
                    "code":         code_block,
                    "lang":         lang or "",
                    "source_url":   url,
                    "collected_at": now,
                })
    else:
        # Text / QA domain: keep legacy prompt/completion format
        meta = {
            "source_url":   url,
            "domain":       domain,
            "task_type":    task_type,
            "collected_at": now,
        }
        if task_type in ("migration", "refactoring", "translation"):
            for i in range(0, len(blocks) - 1, 2):
                recs.append({"prompt": blocks[i], "completion": blocks[i + 1], **meta})
        elif task_type in ("summarization", "documentation"):
            for block in blocks:
                recs.append({"prompt": block, "completion": "", **meta})
        else:
            for i in range(0, len(blocks) - 1, 2):
                recs.append({"prompt": blocks[i], "completion": blocks[i + 1], **meta})

    return recs


# Keep old name as alias for backward compatibility with any existing call sites
def _build_pairs(blocks: list[str], url: str, domain: str, task_type: str) -> list[dict[str, Any]]:
    return _build_records(blocks, url, domain, task_type)


class WebCollector:
    def __init__(self, project: str, domain: str, task_type: str, lang: str | None = None):
        self.project   = project
        self.domain    = domain
        self.task_type = task_type
        self.lang      = lang  # language hint for code domain (neutral schema)

    async def collect(self, topic: str, max_pages: int = 20) -> list[dict[str, Any]]:
        if not _check_crawl4ai():
            return []

        urls = _resolve_urls(self.domain, topic)[:max_pages]
        logger.info(
            "Collecting for domain='%s' topic='%s' lang='%s' — %d sources",
            self.domain, topic, self.lang or "unset", len(urls),
        )

        records: list[dict[str, Any]] = []
        for i, (url, tier) in enumerate(urls):
            logger.debug("[%d/%d] %s (%s)", i + 1, len(urls), url, tier)
            content = await _crawl_url(url)
            if content:
                blocks = _extract_text_blocks(content)
                new_recs = _build_records(blocks, url, self.domain, self.task_type, self.lang)
                logger.info("  → %d blocks → %d records from %s", len(blocks), len(new_recs), tier)
                records.extend(new_recs)
            await asyncio.sleep(_RATE_LIMIT_S)

        logger.info("Total collected: %d records", len(records))
        return records

    def run(self, topic: str, max_pages: int, output: Path, adapter_name: str | None = None) -> int:
        records = asyncio.run(self.collect(topic, max_pages))
        if adapter_name:
            for r in records:
                r["adapter_name"] = adapter_name
        if records:
            from pipeline.utils.jsonl import read_jsonl  # noqa: PLC0415
            existing = read_jsonl(output) if output.exists() else []
            # Dedup key: use (description+code) for neutral schema, (prompt+completion) for legacy
            def _key(r: dict) -> tuple:
                return (
                    r.get("description", "") + r.get("code", "")
                    or r.get("prompt", "") + r.get("completion", "")
                )
            seen = {_key(r) for r in existing}
            new_unique = [r for r in records if _key(r) not in seen]
            if len(records) - len(new_unique):
                logger.info("Deduplicated %d records already in file", len(records) - len(new_unique))
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d records to %s (total: %d)",
                        len(new_unique), output, len(existing) + len(new_unique))
        return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect training pairs from domain sources")
    parser.add_argument("--project",      required=True)
    parser.add_argument("--topic",        required=True)
    parser.add_argument("--output",       required=True)
    parser.add_argument("--domain",       default=None, help="Override domain from project config")
    parser.add_argument("--task-type",    default=None, dest="task_type", help="Override task_type from project config")
    parser.add_argument("--max-pages", "--max-pairs", type=int, default=20, dest="max_pages")
    parser.add_argument("--lang",          default=None, help="Language hint for code domain (e.g. python, java)")
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    args = parser.parse_args()

    cfg_path = Path("config/projects") / f"{args.project}.yaml"
    config: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open() as fh:
            config = yaml.safe_load(fh) or {}

    domain    = args.domain    or config.get("domain",    "code")
    task_type = args.task_type or config.get("task_type", "generation")

    collector = WebCollector(
        project=args.project, domain=domain, task_type=task_type, lang=args.lang
    )
    n = collector.run(
        topic=args.topic, max_pages=args.max_pages,
        output=Path(args.output), adapter_name=args.adapter_name,
    )
    print(f"Collected {n} records → {args.output}")


if __name__ == "__main__":
    main()
