"""Rosetta Code collector: extracts aligned Python/Java training pairs.

Strategy: use MediaWiki API to fetch raw wikitext — no crawl4ai, no browser.
Rosetta Code encodes code blocks as:
  =={{header|Python}}==
  <syntaxhighlight lang="python">...code...</syntaxhighlight>
The wikitext API returns this verbatim, making extraction deterministic.
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


def _fetch_task_list(client: httpx.Client, limit: int = 500) -> list[str]:
    """Return task names from Category:Programming_Tasks via paginated API."""
    tasks: list[str] = []
    params: dict[str, Any] = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": "Category:Programming_Tasks",
        "cmlimit": "500",
        "cmnamespace": "0",
        "format": "json",
    }
    while True:
        r = client.get(_ROSETTA_API, params=params)
        r.raise_for_status()
        data = r.json()
        tasks.extend(m["title"] for m in data.get("query", {}).get("categorymembers", []))
        cont = data.get("continue")
        if not cont or len(tasks) >= limit:
            break
        params.update(cont)
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
    TARGET_LANGS = ("Python", "Java")

    def collect(self, topic: str = "all", max_tasks: int = 100) -> list[dict[str, Any]]:
        """Synchronous collection using MediaWiki API — no crawl4ai required."""
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "mlops-rosetta-collector/1.0 (training data)"},
            follow_redirects=True,
        ) as client:
            logger.info("Fetching Rosetta Code task list (limit=%d)…", max_tasks * 3)
            try:
                all_tasks = _fetch_task_list(client, limit=max_tasks * 3)
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
            logger.info("Processing %d tasks…", len(tasks_to_process))

            pairs: list[dict[str, Any]] = []
            for i, task_name in enumerate(tasks_to_process):
                wikitext = _fetch_wikitext(client, task_name)
                if not wikitext:
                    logger.debug("[%d/%d] ✗ %s — no wikitext",
                                 i + 1, len(tasks_to_process), task_name)
                    continue

                py_code   = _extract_lang_from_wikitext(wikitext, "Python")
                java_code = _extract_lang_from_wikitext(wikitext, "Java")

                if py_code and java_code:
                    pairs.append({
                        "prompt":         py_code,
                        "completion":     java_code,
                        "source_url":     _ROSETTA_BASE + task_name.replace(" ", "_"),
                        "task_name":      task_name,
                        "domain":         "code",
                        "task_type":      "migration",
                        "collector_type": "rosetta_code",
                        "collected_at":   datetime.now(timezone.utc).isoformat(),
                    })
                    logger.info("[%d/%d] ✓ %s (%d / %d chars)",
                                i + 1, len(tasks_to_process), task_name,
                                len(py_code), len(java_code))
                else:
                    missing = [lng for lng, c in (("Python", py_code), ("Java", java_code)) if not c]
                    logger.debug("[%d/%d] ✗ %s — missing: %s",
                                 i + 1, len(tasks_to_process), task_name, missing)

                time.sleep(_RATE_LIMIT_S)

            logger.info("Collected %d aligned Python/Java pairs", len(pairs))
            return pairs

    def run(self, topic: str, max_tasks: int, output: Path, adapter_name: str | None = None) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        pairs = self.collect(topic, max_tasks)
        if adapter_name:
            for p in pairs:
                p["adapter_name"] = adapter_name
        if pairs:
            existing = []
            if output.exists():
                from pipeline.utils.jsonl import read_jsonl  # noqa: PLC0415
                existing = read_jsonl(output)
            seen = {(r.get("prompt", ""), r.get("completion", "")) for r in existing}
            new_unique = [p for p in pairs if (p.get("prompt", ""), p.get("completion", "")) not in seen]
            dupes = len(pairs) - len(new_unique)
            if dupes:
                logger.info("Deduplicated %d pairs already in file", dupes)
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d pairs → %s (total: %d)", len(new_unique), output, len(existing) + len(new_unique))
        return len(pairs)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Python/Java pairs from Rosetta Code")
    parser.add_argument("--topic",        default="all", help="Keyword filter on task names, or 'all'")
    parser.add_argument("--max-tasks",    type=int, default=100, dest="max_tasks")
    parser.add_argument("--output",       required=True, help="Output JSONL file path")
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    args = parser.parse_args()

    collector = RosettaCollector()
    n = collector.run(topic=args.topic, max_tasks=args.max_tasks, output=Path(args.output), adapter_name=args.adapter_name)
    print(f"Collected {n} pairs → {args.output}")


if __name__ == "__main__":
    main()
