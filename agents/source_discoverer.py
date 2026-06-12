"""Source discoverer: autonomously find bilingual documentation sources for a language pair.

AWA (Autonomous Web Agent) component.

Strategy:
  For each concept_keyword in the language pair config:
    - Search "<keyword> <source_lang> tutorial code example" on trusted domains
    - Search "<keyword> <target_lang> tutorial code example" on trusted domains
    - Return URLs that contain actual code blocks in the expected language

Trusted domains are configured in config/trusted_domains.yaml.
Zero hardcoded language names or URLs — fully config-driven.

Output: YAML of discovered sources, ready to be added to project source_configs.

Usage:
    python -m agents.source_discoverer \\
        --pair java-python \\
        --project migrator \\
        --adapter java-to-python-translator \\
        --max-sources 10 \\
        --output discovered_sources.yaml
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.source_discoverer")

_RATE_LIMIT_S     = 1.5   # seconds between search requests
_RATE_LIMIT_MAX   = 8.0   # max backoff after 403
_CODE_MIN_CHARS   = 30    # min code block size to consider a page as having code
_HTTP_TIMEOUT     = 15
_DUCKDUCKGO_URL   = "https://html.duckduckgo.com/html/"
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_SEARXNG_URL      = os.environ.get("SEARXNG_URL", "http://localhost:8889")


# ── Config loaders ────────────────────────────────────────────────────────────

def _load_languages() -> dict[str, Any]:
    path = Path("config/languages.yaml")
    if not path.exists():
        raise FileNotFoundError("config/languages.yaml not found")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _load_trusted_domains() -> dict[str, list[str]]:
    path = Path("config/trusted_domains.yaml")
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _load_project_config(project_id: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Project config not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


# ── Already-known sources ─────────────────────────────────────────────────────

def _known_urls(project_id: str, adapter_name: str) -> set[str]:
    """Return URLs already declared in source_configs for this adapter."""
    try:
        cfg = _load_project_config(project_id)
    except FileNotFoundError:
        return set()
    for adapter in cfg.get("adapters", []):
        if adapter.get("name") == adapter_name:
            return {
                s.get("value", "")
                for s in adapter.get("source_configs", [])
                if s.get("value", "").startswith("http")
            }
    return set()


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

# Rotating User-Agent pool to reduce DuckDuckGo rate-limiting
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]
_ua_idx = 0


def _next_ua() -> str:
    global _ua_idx  # noqa: PLW0603
    ua = _USER_AGENTS[_ua_idx % len(_USER_AGENTS)]
    _ua_idx += 1
    return ua


def _searxng_search(query: str, client: httpx.Client) -> list[str]:
    """
    Search via local SearXNG instance (aggregates Google, Bing, DuckDuckGo, GitHub...).
    Zero API key, zero rate-limit, self-hosted.
    Returns empty list if SearXNG not running.
    """
    try:
        resp = client.get(
            f"{_SEARXNG_URL}/search",
            params={"q": query, "format": "json", "language": "en"},
            headers={"User-Agent": _next_ua()},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        urls = [r["url"] for r in data.get("results", []) if r.get("url")]
        logger.debug("SearXNG: %d results for '%s'", len(urls), query[:60])
        return urls[:15]
    except Exception as exc:
        logger.debug("SearXNG unavailable (%s) — will try fallback", exc)
        return []


def _brave_search(query: str, client: httpx.Client) -> list[str]:
    """
    Search via Brave Search API.
    Reads BRAVE_SEARCH_API_KEY from environment (optional — free tier: 2000 req/month).
    Returns empty list if no key configured.
    """
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        resp = client.get(
            _BRAVE_SEARCH_URL,
            params={"q": query, "count": 10, "safesearch": "off"},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        urls = [r["url"] for r in data.get("web", {}).get("results", []) if r.get("url")]
        logger.debug("Brave Search: %d results for '%s'", len(urls), query[:60])
        return urls
    except Exception as exc:
        logger.debug("Brave Search failed for '%s': %s", query[:60], exc)
        return []


def _search(query: str, client: httpx.Client) -> list[str]:
    """
    Multi-engine search with automatic fallback chain.
    Priority: SearXNG (local) → DuckDuckGo → Brave Search API.

    SearXNG aggregates Google, Bing, DuckDuckGo, GitHub in one call — zero API key,
    no rate-limiting, self-hosted. Start it with: docker compose up searxng -d
    """
    # 1. SearXNG (preferred — local, multi-engine, no rate-limit)
    urls = _searxng_search(query, client)
    if urls:
        logger.debug("  → SearXNG: %d results", len(urls))
        return urls

    # 2. DuckDuckGo (fallback if SearXNG not running)
    urls = _duckduckgo_search(query, client)
    if urls:
        return urls

    # 3. Brave Search API (last resort — requires BRAVE_SEARCH_API_KEY in env)
    brave_urls = _brave_search(query, client)
    if brave_urls:
        logger.info("  → Brave Search fallback: %d results", len(brave_urls))
        return brave_urls

    return []


def _duckduckgo_search(query: str, client: httpx.Client, retries: int = 2) -> list[str]:
    """
    Search DuckDuckGo HTML endpoint with rotating UA and exponential backoff on 403.
    The site: filter is NOT included in the query — filtering is done post-search
    via _is_trusted() to avoid DuckDuckGo returning empty results for narrow queries.
    """
    for attempt in range(retries + 1):
        try:
            resp = client.post(
                _DUCKDUCKGO_URL,
                data={"q": query, "b": "", "kl": "wt-wt"},
                headers={
                    "User-Agent": _next_ua(),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://duckduckgo.com/",
                },
                follow_redirects=True,
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:
            is_403 = "403" in str(exc)
            if attempt < retries:
                wait = _RATE_LIMIT_S * (2 ** attempt) if is_403 else _RATE_LIMIT_S
                wait = min(wait, _RATE_LIMIT_MAX)
                logger.warning("DuckDuckGo search failed (attempt %d/%d) for '%s': %s — retrying in %.1fs",
                               attempt + 1, retries + 1, query[:60], exc, wait)
                time.sleep(wait)
                continue
            logger.warning("DuckDuckGo search failed for '%s': %s", query[:60], exc)
            return []

        # Extract result URLs — try class-based first, then href fallback
        urls = re.findall(r'<a[^>]+class="result__url"[^>]*>([^<]+)</a>', resp.text)
        hrefs = re.findall(r'href="(https?://[^"&]+)"', resp.text)
        all_urls: list[str] = []
        for url in urls:
            url = url.strip()
            if url.startswith("http"):
                all_urls.append(url)
        for url in hrefs:
            if url not in all_urls and "duckduckgo" not in url and "duck.co" not in url:
                all_urls.append(url)
        return all_urls[:15]  # More candidates for trusted-domain filtering

    return []


# ── Page code detection ───────────────────────────────────────────────────────

def _has_code_blocks(html: str, lang: str) -> bool:
    """Quick check: does the page contain code blocks in the expected language?"""
    # Look for fenced code blocks or syntax-highlighted blocks
    patterns = [
        rf'```{re.escape(lang)}',
        rf'class="language-{re.escape(lang)}"',
        rf'lang="{re.escape(lang)}"',
        r'<pre[^>]*>',
        r'<code[^>]*>',
    ]
    code_hints = sum(1 for p in patterns if re.search(p, html, re.IGNORECASE))
    # Also check for language-specific keywords as a fallback
    lang_keywords = {
        "python": ["def ", "import ", "class ", "print("],
        "java":   ["public class", "public static", "import java", "System.out"],
        "javascript": ["function ", "const ", "let ", "var "],
        "typescript": ["interface ", "type ", ": string", ": number"],
    }
    kw_list = lang_keywords.get(lang.lower(), [])
    kw_hits = sum(1 for kw in kw_list if kw in html)
    return code_hints >= 2 or kw_hits >= 2


def _fetch_and_verify(url: str, lang: str, client: httpx.Client) -> bool:
    """
    Fetch page and verify it contains code in the expected language.

    Strategy:
    1. httpx (fast, works for most pages)
    2. Crawl4AI (bypasses JS/Cloudflare/bot detection) on 403 or failure
    """
    html: str | None = None

    # Fast path: httpx
    try:
        resp = client.get(
            url,
            headers={"User-Agent": _next_ua()},
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT,
        )
        if resp.status_code != 403:
            resp.raise_for_status()
            html = resp.text
    except Exception:
        pass

    # Crawl4AI fallback — handles JS rendering + Cloudflare
    if not html:
        try:
            import asyncio as _asyncio  # noqa: PLC0415
            from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig  # noqa: PLC0415

            async def _crawl() -> str | None:
                cfg = BrowserConfig(headless=True, user_agent=_next_ua())
                run_cfg = CrawlerRunConfig(page_timeout=15000, wait_until="domcontentloaded")
                async with AsyncWebCrawler(config=cfg) as crawler:
                    result = await crawler.arun(url=url, config=run_cfg)
                    return result.html if result.success else None

            html = _asyncio.run(_crawl())
            if html:
                logger.debug("  Crawl4AI success: %s", url)
        except Exception as exc:
            logger.debug("  Crawl4AI failed for %s: %s", url, exc)

    if not html:
        return False
    return _has_code_blocks(html, lang)


# ── Domain filtering ──────────────────────────────────────────────────────────

def _is_trusted(url: str, trusted_domains: dict[str, list[str]]) -> bool:
    """Check if URL is from a trusted domain."""
    try:
        domain = urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return False
    all_trusted = set()
    for tier_domains in trusted_domains.values():
        if isinstance(tier_domains, list):
            all_trusted.update(tier_domains)
    return any(domain == td or domain.endswith("." + td) for td in all_trusted)


# ── Main discoverer ───────────────────────────────────────────────────────────

class SourceDiscoverer:
    """
    Discover bilingual documentation sources for a language pair.
    Uses concept_keywords from languages.yaml to build search queries.
    Filters by trusted_domains.yaml.
    """

    def __init__(
        self,
        source_lang: str,
        target_lang: str,
        concept_keywords: list[str],
        trusted_domains: dict[str, list[str]],
        max_sources_per_lang: int = 5,
        adapter_keywords: list[str] | None = None,
    ):
        self.source_lang          = source_lang
        self.target_lang          = target_lang
        self.concept_keywords     = concept_keywords
        self.trusted_domains      = trusted_domains
        self.max_sources_per_lang = max_sources_per_lang
        # Adapter-specific keywords enrich the search (e.g. "JUnit", "Spring", "JPA")
        self.adapter_keywords     = adapter_keywords or []

    def _build_query(self, keyword: str, lang: str) -> str:
        """Build a clean search query — no site: filter to avoid DuckDuckGo empty results."""
        return f"{keyword} {lang} tutorial code example"

    def discover(
        self,
        known_urls: set[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """
        Returns discovered sources grouped by language:
        {"java": [{url, label, lang_hint, concept}, ...], "python": [...]}
        """
        known = known_urls or set()
        results: dict[str, list[dict[str, Any]]] = {
            self.source_lang: [],
            self.target_lang: [],
        }

        # Combine generic + adapter-specific keywords, deduplicated
        all_keywords = list(dict.fromkeys(self.adapter_keywords + self.concept_keywords))

        with httpx.Client(verify=False) as client:
            for lang in (self.source_lang, self.target_lang):
                found = results[lang]

                for keyword in all_keywords:
                    if len(found) >= self.max_sources_per_lang:
                        break

                    query = self._build_query(keyword, lang)
                    logger.info("Searching: '%s'", query)

                    urls = _search(query, client)
                    time.sleep(_RATE_LIMIT_S)

                    for url in urls:
                        if len(found) >= self.max_sources_per_lang:
                            break
                        if url in known:
                            continue
                        if not _is_trusted(url, self.trusted_domains):
                            continue
                        # Already found this URL for this lang
                        if any(s["url"] == url for s in found):
                            continue

                        logger.info("  Verifying %s…", url)
                        if _fetch_and_verify(url, lang, client):
                            found.append({
                                "url":       url,
                                "label":     f"auto-discovered — {keyword} ({lang})",
                                "lang_hint": lang,
                                "concept":   keyword,
                            })
                            logger.info("  ✓ accepted: %s", url)
                        else:
                            logger.debug("  ✗ no code blocks: %s", url)

                        time.sleep(_RATE_LIMIT_S)

        return results

    def to_source_configs(
        self,
        discovered: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Convert discovered URLs to source_config entries for project YAML."""
        entries = []
        for lang, sources in discovered.items():
            for s in sources:
                entries.append({
                    "label":          s["label"],
                    "collector_type": "doc_crawl",
                    "value":          s["url"],
                    "lang_hint":      s["lang_hint"],
                    "estimated_pairs": 50,
                    "auto_discovered": True,
                    "concept":        s.get("concept"),
                })
        return entries


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    lang_cfg  = _load_languages()
    pairs_cfg = lang_cfg.get("pairs", {})

    parser = argparse.ArgumentParser(
        description="Discover bilingual documentation sources for a language pair"
    )
    parser.add_argument("--pair",         required=True, choices=list(pairs_cfg.keys()),
                        help="Language pair key from config/languages.yaml")
    parser.add_argument("--project",      default=None,
                        help="Project ID — used to avoid re-discovering known sources")
    parser.add_argument("--adapter",      default=None,
                        help="Adapter name — used to filter known sources")
    parser.add_argument("--max-sources",  type=int, default=5, dest="max_sources",
                        help="Max new sources to discover per language")
    parser.add_argument("--output",       required=True,
                        help="Output YAML file for discovered sources")
    args = parser.parse_args()

    pcfg     = pairs_cfg[args.pair]
    keywords = pcfg.get("concept_keywords", [])
    if not keywords:
        logger.warning("No concept_keywords defined for pair '%s' in languages.yaml", args.pair)

    trusted = _load_trusted_domains()
    known   = (
        _known_urls(args.project, args.adapter)
        if args.project and args.adapter
        else set()
    )

    # Extract adapter-specific keywords from the adapter description in project config
    adapter_keywords: list[str] = []
    if args.project and args.adapter:
        try:
            project_cfg = _load_project_config(args.project)
            for adapter in project_cfg.get("adapters", []):
                if adapter.get("name") == args.adapter:
                    desc = adapter.get("description", "")
                    all_text = desc + " ".join(str(dt) for dt in adapter.get("data_types", []))
                    import re as _re  # noqa: PLC0415
                    # Extract known tech terms that are meaningful in BOTH languages
                    # These terms produce relevant results regardless of which language is searched
                    tech_terms = _re.findall(
                        r'\b(JPA|JUnit|pytest|Spring|Hibernate|SQLAlchemy|Mockito|FastAPI|'
                        r'Pydantic|LangGraph|ORM|annotation|entity|repository|mapping|'
                        r'configuration|properties|fixture|assertion|interface|inheritance)\b',
                        all_text,
                        _re.IGNORECASE,
                    )
                    # Deduplicate preserving order, lowercase for consistency
                    seen: set[str] = set()
                    for t in tech_terms:
                        tl = t.lower()
                        if tl not in seen:
                            seen.add(tl)
                            adapter_keywords.append(tl)
                    adapter_keywords = adapter_keywords[:8]
                    break
        except Exception as exc:
            logger.debug("Could not extract adapter keywords: %s", exc)

    if adapter_keywords:
        logger.info("Adapter-specific keywords: %s", adapter_keywords)

    discoverer = SourceDiscoverer(
        source_lang=pcfg["source_lang"],
        target_lang=pcfg["target_lang"],
        concept_keywords=keywords,
        trusted_domains=trusted,
        max_sources_per_lang=args.max_sources,
        adapter_keywords=adapter_keywords,
    )

    logger.info(
        "Discovering sources for %s→%s, %d keywords, %d known URLs to skip",
        pcfg["source_lang"], pcfg["target_lang"], len(keywords), len(known),
    )
    discovered = discoverer.discover(known_urls=known)

    total = sum(len(v) for v in discovered.values())
    logger.info("Found %d new sources total", total)

    for lang, sources in discovered.items():
        for s in sources:
            print(f"  [{lang}] {s['concept']}: {s['url']}")

    source_configs = discoverer.to_source_configs(discovered)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        yaml.dump({"discovered_sources": source_configs}, allow_unicode=True, sort_keys=False)
    )
    print(f"\n{total} sources → {args.output}")
    print("Add them to your adapter source_configs in config/projects/<project>.yaml")


if __name__ == "__main__":
    main()
