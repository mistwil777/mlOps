"""Rosetta Code collector: extracts aligned Python/Java training pairs."""

from __future__ import annotations

import argparse
import asyncio
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from pipeline.utils.jsonl import write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.rosetta_collector")

_ROSETTA_API   = "https://rosettacode.org/w/api.php"
_ROSETTA_BASE  = "https://rosettacode.org/wiki/"
_RATE_LIMIT_S  = 1.5   # polite delay between requests
_CRAWL_TIMEOUT = 30
_MIN_CODE_CHARS = 30   # ignore trivial snippets


def _fetch_task_list(limit: int = 500) -> list[str]:
    """Return task names from Rosetta Code via the MediaWiki API."""
    tasks: list[str] = []
    params: dict[str, Any] = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Programming_Tasks",
        "cmlimit": "500",
        "cmnamespace": "0",
        "format": "json",
    }
    with httpx.Client(timeout=30) as client:
        while True:
            r = client.get(_ROSETTA_API, params=params)
            r.raise_for_status()
            data = r.json()
            members = data.get("query", {}).get("categorymembers", [])
            tasks.extend(m["title"] for m in members)
            cont = data.get("continue")
            if not cont or len(tasks) >= limit:
                break
            params.update(cont)
    return tasks[:limit]


async def _crawl_page(url: str) -> str | None:
    """Crawl a Rosetta Code task page and return markdown content."""
    try:
        from crawl4ai import AsyncWebCrawler  # noqa: PLC0415
        from crawl4ai.async_configs import CrawlerRunConfig  # noqa: PLC0415

        cfg = CrawlerRunConfig(page_timeout=_CRAWL_TIMEOUT * 1_000)
        async with AsyncWebCrawler(verbose=False) as crawler:
            result = await asyncio.wait_for(
                crawler.arun(url=url, config=cfg),
                timeout=_CRAWL_TIMEOUT,
            )
            return result.markdown if result.success else None
    except asyncio.TimeoutError:
        logger.warning("Timeout crawling %s", url)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Crawl error %s: %s", url, exc)
        return None


def _extract_lang_section(markdown: str, lang: str) -> str | None:
    """
    Extract the first code block from the language section of a Rosetta Code page.

    Rosetta Code sections rendered as markdown look like:
        ## Python
        Some description...
        ```python
        <code>
        ```
    or equivalently with =Python= mediawiki headings converted to ## Python.
    """
    # Find section header (case-insensitive, ## Python or ## Python 2/3 etc.)
    pattern = rf"(?:^|\n)##\s*{re.escape(lang)}[\w\s/]*\n(.*?)(?=\n##\s|\Z)"
    m = re.search(pattern, markdown, re.IGNORECASE | re.DOTALL)
    if not m:
        return None

    section = m.group(1)

    # Extract first fenced code block in that section
    code_m = re.search(r"```[^\n]*\n(.*?)```", section, re.DOTALL)
    if code_m:
        code = code_m.group(1).strip()
        if len(code) >= _MIN_CODE_CHARS:
            return code

    # Fallback: look for indented code (4-space or tab-indented lines)
    lines = section.splitlines()
    code_lines = [ln[4:] if ln.startswith("    ") else ln[1:] if ln.startswith("\t") else None
                  for ln in lines]
    code_block = "\n".join(ln for ln in code_lines if ln is not None).strip()
    if len(code_block) >= _MIN_CODE_CHARS:
        return code_block

    return None


class RosettaCollector:
    TARGET_LANGS = ("Python", "Java")

    async def collect(self, topic: str = "all", max_tasks: int = 100) -> list[dict[str, Any]]:
        logger.info("Fetching Rosetta Code task list (limit=%d)…", max_tasks * 3)
        try:
            all_tasks = _fetch_task_list(limit=max_tasks * 3)
        except Exception as exc:
            logger.error("Failed to fetch task list: %s", exc)
            return []

        # Filter by keyword if requested
        if topic != "all":
            keyword = topic.lower().replace("-", " ")
            filtered = [t for t in all_tasks if keyword in t.lower()]
            logger.info("Filtered %d → %d tasks matching '%s'", len(all_tasks), len(filtered), topic)
        else:
            filtered = all_tasks

        tasks_to_process = filtered[:max_tasks]
        logger.info("Processing %d tasks…", len(tasks_to_process))

        pairs: list[dict[str, Any]] = []
        for i, task_name in enumerate(tasks_to_process):
            url = _ROSETTA_BASE + task_name.replace(" ", "_")
            markdown = await _crawl_page(url)
            if not markdown:
                continue

            py_code   = _extract_lang_section(markdown, "Python")
            java_code = _extract_lang_section(markdown, "Java")

            if py_code and java_code:
                pairs.append({
                    "prompt":         py_code,
                    "completion":     java_code,
                    "source_url":     url,
                    "task_name":      task_name,
                    "domain":         "code",
                    "task_type":      "migration",
                    "collector_type": "rosetta_code",
                    "collected_at":   datetime.now(timezone.utc).isoformat(),
                })
                logger.info("[%d/%d] ✓ %s (%dp, %dc chars)",
                            i + 1, len(tasks_to_process), task_name,
                            len(py_code), len(java_code))
            else:
                missing = [l for l, c in (("Python", py_code), ("Java", java_code)) if not c]
                logger.debug("[%d/%d] ✗ %s — missing: %s",
                             i + 1, len(tasks_to_process), task_name, missing)

            await asyncio.sleep(_RATE_LIMIT_S)

        logger.info("Collected %d aligned Python/Java pairs", len(pairs))
        return pairs

    def run(self, topic: str, max_tasks: int, output: Path) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        pairs = asyncio.run(self.collect(topic, max_tasks))
        if pairs:
            write_jsonl(output, pairs)
            logger.info("Written %d pairs → %s", len(pairs), output)
        return len(pairs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Python/Java pairs from Rosetta Code")
    parser.add_argument("--topic",     default="all", help="Keyword filter on task names, or 'all'")
    parser.add_argument("--max-tasks", type=int, default=100, dest="max_tasks")
    parser.add_argument("--output",    required=True, help="Output JSONL file path")
    args = parser.parse_args()

    collector = RosettaCollector()
    n = collector.run(topic=args.topic, max_tasks=args.max_tasks, output=Path(args.output))
    print(f"Collected {n} pairs → {args.output}")


if __name__ == "__main__":
    main()
