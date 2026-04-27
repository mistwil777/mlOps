"""Web collector agent: crawl domain sources and generate training pairs."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
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
    """
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


async def _crawl_url(url: str) -> str | None:
    """Crawl a single URL and return markdown content."""
    try:
        from crawl4ai import AsyncWebCrawler  # noqa: PLC0415
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(url=url)
            return result.markdown if result.success else None
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


def _build_pairs(blocks: list[str], url: str, domain: str, task_type: str) -> list[dict[str, Any]]:
    """
    Build (prompt, completion) pairs from extracted blocks.
    Strategy depends on task_type:
      - migration / refactoring / translation : consecutive pairs (input → output)
      - summarization / documentation        : (full text → summary placeholder)
      - others                               : consecutive pairs as generic QA
    """
    pairs: list[dict[str, Any]] = []
    meta = {
        "source_url":    url,
        "domain":        domain,
        "task_type":     task_type,
        "collected_at":  datetime.utcnow().isoformat(),
    }

    if task_type in ("migration", "refactoring", "translation"):
        for i in range(0, len(blocks) - 1, 2):
            pairs.append({"prompt": blocks[i], "completion": blocks[i + 1], **meta})
    elif task_type in ("summarization", "documentation"):
        for block in blocks:
            pairs.append({"prompt": block, "completion": "", **meta})  # completion filled by augmentation
    else:
        for i in range(0, len(blocks) - 1, 2):
            pairs.append({"prompt": blocks[i], "completion": blocks[i + 1], **meta})

    return pairs


class WebCollector:
    def __init__(self, project: str, domain: str, task_type: str):
        self.project   = project
        self.domain    = domain
        self.task_type = task_type

    async def collect(self, topic: str, max_pages: int = 20) -> list[dict[str, Any]]:
        if not _check_crawl4ai():
            return []

        urls = _resolve_urls(self.domain, topic)[:max_pages]
        logger.info(
            "Collecting for domain='%s' topic='%s' — %d sources",
            self.domain, topic, len(urls),
        )

        pairs: list[dict[str, Any]] = []
        for i, (url, tier) in enumerate(urls):
            logger.debug("[%d/%d] %s (%s)", i + 1, len(urls), url, tier)
            content = await _crawl_url(url)
            if content:
                blocks = _extract_text_blocks(content)
                new_pairs = _build_pairs(blocks, url, self.domain, self.task_type)
                logger.info("  → %d blocks → %d pairs from %s", len(blocks), len(new_pairs), tier)
                pairs.extend(new_pairs)
            await asyncio.sleep(_RATE_LIMIT_S)

        logger.info("Total collected: %d raw pairs", len(pairs))
        return pairs

    def run(self, topic: str, max_pages: int, output: Path) -> int:
        pairs = asyncio.run(self.collect(topic, max_pages))
        if pairs:
            write_jsonl(output, pairs)
            logger.info("Written %d pairs to %s", len(pairs), output)
        return len(pairs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect training pairs from domain sources")
    parser.add_argument("--project",   required=True, help="Project ID (matches config/projects/<id>.yaml)")
    parser.add_argument("--topic",     required=True, help="Topic within the domain (e.g. 'python', 'french_law')")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--output",    required=True)
    args = parser.parse_args()

    with open(Path("config/projects") / f"{args.project}.yaml") as fh:
        config = yaml.safe_load(fh)

    collector = WebCollector(
        project=args.project,
        domain=config.get("domain", "code"),
        task_type=config.get("task_type", "generation"),
    )
    n = collector.run(
        topic=args.topic,
        max_pages=args.max_pages,
        output=Path(args.output),
    )
    print(f"Collected {n} pairs → {args.output}")


if __name__ == "__main__":
    main()
