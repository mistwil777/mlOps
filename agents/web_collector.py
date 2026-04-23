"""Web collector agent: crawl code examples and generate training pairs."""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime
from pathlib import Path

from pipeline.utils.jsonl import write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.web_collector")

_GITHUB_SEARCH_URL = "https://github.com/search?q={query}&type=code"
_RATE_LIMIT_SECONDS = 1.0


def _check_crawl4ai() -> bool:
    try:
        import crawl4ai  # noqa: F401
        return True
    except ImportError:
        logger.warning("crawl4ai not installed. Run: pip install crawl4ai")
        return False


async def _crawl_url(url: str, max_retries: int = 2) -> str | None:
    """Crawl a single URL and return markdown content."""
    try:
        from crawl4ai import AsyncWebCrawler  # noqa: PLC0415

        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await crawler.arun(url=url)
            if result.success:
                return result.markdown
            return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Crawl failed for %s: %s", url, exc)
        return None


def _extract_code_blocks(markdown: str, src_ext: str = ".py") -> list[str]:
    """Extract fenced code blocks from markdown."""
    import re  # noqa: PLC0415

    pattern = rf"```(?:python|java|javascript|typescript)?[\n](.*?)```"
    blocks = re.findall(pattern, markdown, re.DOTALL)
    return [b.strip() for b in blocks if len(b.strip()) > 50]


def _build_search_urls(src_lang: str, tgt_lang: str, topic: str, max_pages: int) -> list[str]:
    """Generate search URLs for code pair discovery."""
    queries = [
        f"{topic} {src_lang} to {tgt_lang} example",
        f"convert {src_lang} {tgt_lang} {topic}",
        f"{topic} {src_lang} {tgt_lang} translation",
    ]
    urls = []
    for q in queries[:max_pages // 3 + 1]:
        encoded = q.replace(" ", "+")
        urls.append(f"https://github.com/search?q={encoded}&type=code&l={src_lang}")
    return urls[:max_pages]


class WebCollector:
    def __init__(self, project: str, src_lang: str, tgt_lang: str):
        self.project = project
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang

    async def collect(
        self,
        topic: str,
        max_pages: int = 20,
        since: str | None = None,
    ) -> list[dict]:
        if not _check_crawl4ai():
            return []

        urls = _build_search_urls(self.src_lang, self.tgt_lang, topic, max_pages)
        logger.info("Crawling %d URLs for topic: %s", len(urls), topic)

        pairs: list[dict] = []
        for i, url in enumerate(urls):
            logger.debug("Crawling %d/%d: %s", i + 1, len(urls), url)
            content = await _crawl_url(url)
            if content:
                blocks = _extract_code_blocks(content)
                # Pair consecutive blocks as (src, tgt) candidates
                for j in range(0, len(blocks) - 1, 2):
                    pairs.append({
                        "prompt": blocks[j],
                        "completion": blocks[j + 1],
                        "source_url": url,
                        "collected_at": datetime.utcnow().isoformat(),
                    })
            await asyncio.sleep(_RATE_LIMIT_SECONDS)

        logger.info("Collected %d raw pairs from %d pages", len(pairs), len(urls))
        return pairs

    def run(self, topic: str, max_pages: int, output: Path, since: str | None = None) -> int:
        pairs = asyncio.run(self.collect(topic, max_pages, since))

        if pairs:
            write_jsonl(output, pairs)
            logger.info("Written %d pairs to %s", len(pairs), output)
        return len(pairs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--topic", default="")
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--since", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    import yaml  # noqa: PLC0415

    with open(Path("config/projects") / f"{args.project}.yaml") as fh:
        config = yaml.safe_load(fh)

    collector = WebCollector(
        project=args.project,
        src_lang=config["source_lang"],
        tgt_lang=config["target_lang"],
    )
    n = collector.run(
        topic=args.topic or f"{config['source_lang']} to {config['target_lang']}",
        max_pages=args.max_pages,
        output=Path(args.output),
        since=args.since,
    )
    print(f"Collected {n} pairs → {args.output}")


if __name__ == "__main__":
    main()
