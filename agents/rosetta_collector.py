"""Rosetta Code collector: extracts neutral per-language records from wiki tasks.

Strategy: use MediaWiki API to fetch raw wikitext — no crawl4ai, no browser.
Rosetta Code encodes code blocks as:
  =={{header|Python}}==
  <syntaxhighlight lang="python">...code...</syntaxhighlight>

Output format: one record per (task, language) — neutral, no prompt/completion.
  {"task_name": str, "lang": str, "code": str, "source_url": str, "collected_at": str}

The transform step (pipeline/transform.py + config/transforms/code_migration.yaml)
is responsible for joining records into training pairs.
"""

from __future__ import annotations

import argparse
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from pipeline.utils.jsonl import write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.rosetta_collector")

_ROSETTA_API    = "https://rosettacode.org/w/api.php"
_ROSETTA_BASE   = "https://rosettacode.org/wiki/"
_RATE_LIMIT_S   = 1.0
_MIN_CODE_CHARS = 30
_HTTP_TIMEOUT   = 20


def _fetch_task_list(client: httpx.Client, limit: int = 500, topic: str | None = None) -> list[str]:
    """Return task names from Rosetta Code via the MediaWiki API.

    Strategy:
    - With topic: full-text search (srsearch) — covers cases where the category
      API returns empty results due to Rosetta Code API restructuring.
    - Without topic: allpages in namespace 0 as fallback.
    Both paths also try Category:Programming_Tasks for backwards compat.
    """
    tasks: list[str] = []

    # 1. Category-based listing (original approach — may return empty on restructured API)
    params: dict[str, Any] = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Programming_Tasks",
        "cmlimit": "500",
        "cmnamespace": "0",
        "format": "json",
    }
    while len(tasks) < limit:
        r = client.get(_ROSETTA_API, params=params)
        r.raise_for_status()
        data = r.json()
        batch = [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
        tasks.extend(batch)
        cont = data.get("continue")
        if not cont or not batch:
            break
        params.update(cont)

    # 2. Full-text search — always run when a topic is given.
    #    Category listing is alphabetical (not topic-filtered), so search results
    #    are prepended to ensure topic-matching tasks survive the [:limit] slice.
    if topic:
        search_params: dict[str, Any] = {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "srlimit": str(min(limit, 500)),
            "srnamespace": "0",
            "format": "json",
        }
        r = client.get(_ROSETTA_API, params=search_params)
        r.raise_for_status()
        search_results = [
            item["title"]
            for item in r.json().get("query", {}).get("search", [])
            if item["title"] not in tasks
        ]
        # Prepend search results so they are not truncated by [:limit]
        tasks = search_results + tasks

    return tasks[:limit]


def _fetch_wikitext(client: httpx.Client, title: str) -> str | None:
    """Fetch the raw wikitext of a Rosetta Code task page via the API."""
    params = {
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "formatversion": "2",
    }
    try:
        r = client.get(_ROSETTA_API, params=params, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        pages = r.json().get("query", {}).get("pages", [])
        if not pages:
            return None
        slots = pages[0].get("revisions", [{}])[0].get("slots", {})
        return slots.get("main", {}).get("content")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to fetch wikitext for '%s': %s", title, exc)
        return None


def _extract_lang_from_wikitext(wikitext: str, lang: str) -> str | None:
    """
    Extract first code block for a language from Rosetta Code wikitext.

    Rosetta Code format:
        =={{header|Python}}==
        {{works with|Python|3.0}}
        <syntaxhighlight lang="python">def foo(): ...</syntaxhighlight>
    """
    escaped = re.escape(lang)

    # Section boundary: =={{header|<Lang>...}}== up to next =={{header| or end
    section_m = re.search(
        r"=={{header\|" + escaped + r"[^}]*}}==[ \t]*\n(.*?)(?==={{header\||$)",
        wikitext,
        re.IGNORECASE | re.DOTALL,
    )
    if not section_m:
        return None

    section = section_m.group(1)

    # Primary: <syntaxhighlight ...>code</syntaxhighlight>
    sh = re.search(r"<syntaxhighlight[^>]*>(.*?)</syntaxhighlight>", section, re.IGNORECASE | re.DOTALL)
    if sh:
        code = sh.group(1).strip()
        if len(code) >= _MIN_CODE_CHARS:
            return code

    # Fallback: legacy <lang xxx>code</lang>
    lg = re.search(r"<lang\s+\w+[^>]*>(.*?)</lang>", section, re.IGNORECASE | re.DOTALL)
    if lg:
        code = lg.group(1).strip()
        if len(code) >= _MIN_CODE_CHARS:
            return code

    return None


class RosettaCollector:

    def __init__(self, langs: list[str]) -> None:
        """
        Args:
            langs: language names to extract, e.g. ["Python", "Java"] or ["C", "Rust"].
                   At least one entry required. All matching records are emitted.
        """
        if not langs:
            raise ValueError("langs must contain at least one language name")
        self.langs = langs

    def collect(self, topic: str = "all", max_tasks: int = 100) -> list[dict[str, Any]]:
        """Return neutral per-language records — one record per (task, language) found."""
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "mlops-rosetta-collector/1.0 (training data)"},
            follow_redirects=True,
        ) as client:
            logger.info("Fetching Rosetta Code task list (limit=%d)…", max_tasks * 3)
            try:
                search_topic = None if topic == "all" else topic
                all_tasks = _fetch_task_list(client, limit=max_tasks * 3, topic=search_topic)
            except Exception as exc:
                logger.error("Failed to fetch task list: %s", exc)
                return []

            if topic != "all":
                keyword = topic.lower().replace("-", " ")
                filtered = [t for t in all_tasks if keyword in t.lower()]
                logger.info("Filtered %d → %d tasks matching '%s'",
                            len(all_tasks), len(filtered), topic)
            else:
                filtered = all_tasks

            tasks_to_process = filtered[:max_tasks]
            logger.info("Processing %d tasks for langs %s…", len(tasks_to_process), self.langs)

            records: list[dict[str, Any]] = []
            for i, task_name in enumerate(tasks_to_process):
                wikitext = _fetch_wikitext(client, task_name)
                if not wikitext:
                    logger.debug("[%d/%d] ✗ %s — no wikitext",
                                 i + 1, len(tasks_to_process), task_name)
                    time.sleep(_RATE_LIMIT_S)
                    continue

                found: list[str] = []
                for lang in self.langs:
                    code = _extract_lang_from_wikitext(wikitext, lang)
                    if code:
                        records.append({
                            "task_name":    task_name,
                            "lang":         lang.lower(),  # normalize to lowercase for pipeline consistency
                            "code":         code,
                            "source_url":   _ROSETTA_BASE + task_name.replace(" ", "_"),
                            "collected_at": datetime.now(timezone.utc).isoformat(),
                        })
                        found.append(lang)

                if found:
                    logger.info("[%d/%d] ✓ %s — found: %s",
                                i + 1, len(tasks_to_process), task_name, found)
                else:
                    logger.debug("[%d/%d] ✗ %s — no code for %s",
                                 i + 1, len(tasks_to_process), task_name, self.langs)

                time.sleep(_RATE_LIMIT_S)

            logger.info("Collected %d records (%d langs × tasks)", len(records), len(self.langs))
            return records

    def run(
        self,
        topic: str,
        max_tasks: int,
        output: Path,
        adapter_name: str | None = None,
    ) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        records = self.collect(topic, max_tasks)
        if adapter_name:
            for r in records:
                r["adapter_name"] = adapter_name
        if records:
            existing: list[dict[str, Any]] = []
            if output.exists():
                from pipeline.utils.jsonl import read_jsonl  # noqa: PLC0415
                existing = read_jsonl(output)
            seen = {(r.get("task_name", ""), r.get("lang", "")) for r in existing}
            new_unique = [r for r in records if (r["task_name"], r["lang"]) not in seen]
            dupes = len(records) - len(new_unique)
            if dupes:
                logger.info("Deduplicated %d records already in file", dupes)
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d records → %s (total: %d)",
                        len(new_unique), output, len(existing) + len(new_unique))
        return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect per-language records from Rosetta Code (neutral output)")
    parser.add_argument("--langs",       required=True, nargs="+",
                        help="Language names to extract, e.g. --langs Python Java")
    parser.add_argument("--topic",       default="all",
                        help="Keyword filter on task names, or 'all'")
    parser.add_argument("--max-tasks",   type=int, default=100, dest="max_tasks")
    parser.add_argument("--output",      required=True, help="Output JSONL file path")
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    args = parser.parse_args()

    collector = RosettaCollector(langs=args.langs)
    n = collector.run(
        topic=args.topic,
        max_tasks=args.max_tasks,
        output=Path(args.output),
        adapter_name=args.adapter_name,
    )
    print(f"Collected {n} records → {args.output}")


if __name__ == "__main__":
    main()
