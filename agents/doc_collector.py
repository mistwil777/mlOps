"""Documentation collector: crawl official language docs and extract (description, code) pairs.

Strategy:
  - Crawl a doc site recursively (same-origin links only, bounded depth)
  - For each page, extract consecutive (text_before_code_block, code_block) pairs
  - Tag each pair with lang_hint (python | java | typescript | go …)
  - Output JSONL: {description, code, lang, source_url, collected_at}  ← neutral fields

Output is neutral — no prompt/completion. The transform step (pipeline/transform.py +
config/transforms/doc_to_code.yaml) is responsible for producing training pairs.

Custom HTML tags (e.g. java-playground on dev.java) can be handled via --custom-tags
instead of being hardcoded here.
"""

from __future__ import annotations

import argparse
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.doc_collector")

_HTTP_TIMEOUT    = 20
_RATE_LIMIT_S    = 0.8
_MIN_CODE_CHARS  = 30
_MIN_DESC_CHARS  = 20
_MAX_PAGES       = 200
_MAX_DEPTH       = 5

# URL patterns that yield zero code pairs — skip them to avoid wasting crawl budget.
# Applied to the path component of the URL (case-insensitive substring match).
_SKIP_URL_PATTERNS: tuple[str, ...] = (
    "genindex",          # Python doc alphabetical index (genindex-A.html … genindex-Z.html)
    "py-modindex",       # Python module index
    "/archives/",        # downloadable doc bundles (.zip, .epub, .tar, .bz2 …)
    "download.html",     # download pages
    "/search",           # search pages
    "changelog",         # long changelogs — very few code snippets per byte of HTML
    "copyright",         # legal pages
    "license",           # legal pages
    "about.html",        # about/meta pages
    "bugs.html",         # bug reporting pages
    "improve-page",      # feedback widgets
)
_SKIP_URL_EXTENSIONS: tuple[str, ...] = (
    ".zip", ".tar", ".bz2", ".gz", ".epub", ".pdf", ".exe", ".msi",
    ".whl", ".tar.gz", ".tgz", ".xz", ".7z",
)

def _should_skip_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    if any(ext in path for ext in _SKIP_URL_EXTENSIONS):
        return True
    return any(pat in path for pat in _SKIP_URL_PATTERNS)


_SSL_TEST_URL = "https://docs.python.org/fr/3/"
_VERIFY: bool | str = True  # resolved once at module load


def _resolve_ssl_verify() -> bool | str:
    """Determine the best SSL verification strategy for this Python environment.

    Tries in order:
    1. System default (True) — works when Python ships with up-to-date CA bundle
    2. certifi bundle — often available via pip, but may be stale on old envs
    3. verify=False — last resort for known public doc sites on broken CA setups

    The result is cached at module load so we only probe once per process.
    """
    _ua = {"User-Agent": "Mozilla/5.0 (compatible; mlops-doc-collector/1.0)"}

    # 1. system default
    try:
        with httpx.Client(timeout=5, follow_redirects=True, headers=_ua) as c:
            c.head(_SSL_TEST_URL)
        return True
    except Exception:
        pass

    # 2. certifi bundle
    try:
        import certifi  # type: ignore  # noqa: PLC0415
        with httpx.Client(timeout=5, follow_redirects=True, verify=certifi.where(), headers=_ua) as c:
            c.head(_SSL_TEST_URL)
        return certifi.where()
    except Exception:
        pass

    # 3. disable verification — acceptable for known public doc sites
    logger.warning(
        "SSL verification disabled for doc crawl — CA bundle unavailable or expired. "
        "Fix with: pip install --upgrade certifi"
    )
    return False


def _make_http_client(timeout: int = _HTTP_TIMEOUT) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        verify=_VERIFY,
        headers={"User-Agent": "Mozilla/5.0 (compatible; mlops-doc-collector/1.0)"},
    )


# Resolve once at import time (fast — probes a single HEAD request)
try:
    _VERIFY = _resolve_ssl_verify()
except Exception:
    _VERIFY = False

# Code fence patterns for markdown output from crawl4ai
_CODE_FENCE_RE   = re.compile(r"```(?P<lang>[^\n]*)\n(?P<code>.*?)```", re.DOTALL)
# Plain indented code blocks (4-space indent paragraphs)
_INDENT_CODE_RE  = re.compile(r"(?m)^(?: {4}|\t)(.+?)(?=\n\n|\Z)", re.DOTALL)


def _load_languages() -> dict[str, Any]:
    path = Path("config/languages.yaml")
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _same_origin(base_url: str, href: str) -> bool:
    base = urlparse(base_url)
    target = urlparse(href)
    if not target.scheme:
        return True
    return target.netloc == base.netloc


def _normalize_url(base: str, href: str) -> str | None:
    try:
        url = urljoin(base, href)
        parsed = urlparse(url)
        # Drop anchors, keep path + query
        return parsed._replace(fragment="").geturl()
    except Exception:
        return None


def _load_collector_defaults() -> dict[str, Any]:
    """Load skip_langs and real_langs from config/languages.yaml collector_defaults."""
    path = Path("config/languages.yaml")
    if not path.exists():
        return {}
    with path.open() as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg.get("collector_defaults", {})


_COLLECTOR_DEFAULTS: dict[str, Any] = {}


def _get_collector_defaults() -> dict[str, Any]:
    global _COLLECTOR_DEFAULTS
    if not _COLLECTOR_DEFAULTS:
        _COLLECTOR_DEFAULTS = _load_collector_defaults()
    return _COLLECTOR_DEFAULTS


def _extract_pairs_from_markdown(
    markdown: str,
    url: str,
    lang_hint: str,
    custom_tags: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Extract (description, code) pairs from markdown output.

    Strategy:
    1. Split into paragraphs/headings and code fences
    2. For each code fence: take the non-empty text block immediately before it as description (may be empty)
    3. Filter: drop trivially empty fences (< _MIN_CODE_CHARS and < 2 lines)
    4. Skip fences whose lang annotation is a different real language than lang_hint
    Description filtering and quality thresholds are enforced downstream in pipeline/clean_raw.py.

    Returns neutral records: {description, code, lang, source_url, collected_at}
    """
    defaults = _get_collector_defaults()
    skip_langs: set[str] = set(defaults.get("skip_langs", [
        "bash", "shell", "sh", "console", "text", "output",
        "xml", "json", "yaml", "toml", "ini", "css", "html",
        "diff", "makefile", "dockerfile", "sql", "plaintext",
    ]))
    real_langs: set[str] = set(defaults.get("real_langs", [
        "python", "java", "javascript", "typescript", "go", "rust",
        "cpp", "c", "kotlin", "scala", "ruby", "swift", "cs", "php",
    ]))

    pairs: list[dict[str, Any]] = []

    # Split on fenced code blocks — alternate between text and code
    parts = _CODE_FENCE_RE.split(markdown)
    # re.split with groups returns: [text0, lang1, code1, text1, lang2, code2, …]

    i = 0
    pending_text = ""
    while i < len(parts):
        if i % 3 == 0:
            pending_text = parts[i].strip()
        elif i % 3 == 1:
            fence_lang = parts[i].strip().lower()
        elif i % 3 == 2:
            code = parts[i].strip()
            if fence_lang in skip_langs:
                pending_text = ""
                i += 1
                continue
            if fence_lang and fence_lang in real_langs and fence_lang != lang_hint:
                pending_text = ""
                i += 1
                continue

            # Drop trivially empty fences (single token, no real content)
            code_lines = [l for l in code.splitlines() if l.strip()]
            if len(code_lines) < 2 and len(code) < _MIN_CODE_CHARS:
                pending_text = ""
                i += 1
                continue

            # Accept any code block regardless of whether a description precedes it.
            # Description filtering belongs to pipeline/clean_raw.py, not here.
            desc = _best_description(pending_text)
            pairs.append({
                "description":  desc,
                "code":         code,
                "lang":         lang_hint,
                "source_url":   url,
                "collected_at": datetime.now(timezone.utc).isoformat(),
            })
            pending_text = ""
        i += 1

    return pairs


def _best_description(text: str) -> str:
    """Extract the last meaningful paragraph or heading from preceding text."""
    if not text:
        return ""
    # Split into paragraphs, take the last non-empty one (closest to the code)
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if not paragraphs:
        return ""
    # Prefer the last paragraph that is not just a heading marker
    for para in reversed(paragraphs):
        clean = re.sub(r"^#{1,6}\s*", "", para).strip()
        if len(clean) >= _MIN_DESC_CHARS:
            return clean[:500]  # cap at 500 chars
    return paragraphs[-1][:500]


class DocCollector:
    """
    Recursive documentation crawler that extracts (description, code) pairs.
    Stays within the same origin as the seed URL.
    Output: neutral records {description, code, lang, source_url, collected_at}.
    """

    def __init__(
        self,
        lang_hint: str,
        max_pages: int = _MAX_PAGES,
        max_depth: int = _MAX_DEPTH,
        custom_tags: list[str] | None = None,
        use_playwright: bool = False,
    ):
        self.lang_hint      = lang_hint
        self.max_pages      = max_pages
        self.max_depth      = max_depth
        self.custom_tags    = custom_tags or []
        self.use_playwright = use_playwright

    def collect(self, seed_url: str) -> list[dict[str, Any]]:
        visited: set[str] = set()
        seed_path = urlparse(seed_url).path

        queue: list[tuple[str, int]] = [(seed_url, 0)]
        all_pairs: list[dict[str, Any]] = []
        content_count = 0  # pages from which extraction was attempted

        fetch = self._crawl_page_playwright if self.use_playwright else None

        with _make_http_client() as client:
            while queue and content_count < self.max_pages:
                url, depth = queue.pop(0)
                if url in visited:
                    continue
                if _should_skip_url(url):
                    continue
                visited.add(url)

                logger.info("[%d/%d] depth=%d %s",
                            content_count + 1, self.max_pages, depth, url)

                try:
                    if fetch:
                        markdown, links = fetch(url)
                    else:
                        markdown, links = self._crawl_page(client, url)
                except Exception as exc:
                    logger.warning("Failed to crawl %s: %s", url, exc)
                    time.sleep(_RATE_LIMIT_S)
                    continue

                content_count += 1
                pairs = _extract_pairs_from_markdown(
                    markdown, url, self.lang_hint, custom_tags=self.custom_tags)
                all_pairs.extend(pairs)
                if pairs:
                    logger.info("  -> %d records extracted", len(pairs))

                # Follow child links within the seed sub-path
                if depth < self.max_depth:
                    for link in links:
                        norm = _normalize_url(url, link)
                        if not norm or norm in visited:
                            continue
                        if not _same_origin(seed_url, norm):
                            continue
                        link_path = urlparse(norm).path
                        if not link_path.startswith(seed_path):
                            continue
                        if norm == seed_url:
                            continue
                        queue.append((norm, depth + 1))

                time.sleep(_RATE_LIMIT_S)

        logger.info("Total: %d pairs from %d pages (%s)", len(all_pairs), len(visited), seed_url)
        return all_pairs

    def _crawl_page(self, client: httpx.Client, url: str) -> tuple[str, list[str]]:
        """Fetch page with httpx, return (markdown_text, outgoing_links)."""
        r = client.get(url)
        r.raise_for_status()
        html = r.text
        markdown = _html_to_markdown(html, custom_tags=self.custom_tags)
        links = _extract_links(html)
        return markdown, links

    def _crawl_page_playwright(self, url: str) -> tuple[str, list[str]]:
        """Fetch page with a headless Chromium browser — bypasses JS challenges and
        Cloudflare-style bot detection that block plain httpx requests."""
        import random  # noqa: PLC0415
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        _USER_AGENTS = [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        ]

        ua = random.choice(_USER_AGENTS)
        viewport = {
            "width":  random.randint(1280, 1920),
            "height": random.randint(800, 1080),
        }

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=ua,
                viewport=viewport,
                locale="en-US",
            )
            page = context.new_page()

            # playwright-stealth: patches navigator.webdriver, WebGL, canvas fingerprint
            try:
                from playwright_stealth import stealth_sync  # type: ignore  # noqa: PLC0415
                stealth_sync(page)
            except ImportError:
                pass

            # Block images/fonts/media — faster crawl, irrelevant to code extraction
            page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf,mp4,webm}",
                       lambda route: route.abort())
            try:
                # Human-like delay before navigation (0.5–1.5s)
                time.sleep(random.uniform(0.5, 1.5))
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                # Wait for main content to render (Cloudflare challenge clears within ~5s)
                page.wait_for_timeout(2_000)
                html = page.content()
            finally:
                browser.close()

        markdown = _html_to_markdown(html, custom_tags=self.custom_tags)
        links = _extract_links(html)
        time.sleep(_RATE_LIMIT_S)
        return markdown, links

    def run(
        self,
        seed_url: str,
        output: Path,
        adapter_name: str | None = None,
    ) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        pairs = self.collect(seed_url)

        if adapter_name:
            for p in pairs:
                p["adapter_name"] = adapter_name

        if pairs:
            existing = read_jsonl(output) if output.exists() else []
            seen = {(r.get("description", ""), r.get("code", "")) for r in existing}
            new_unique = [p for p in pairs if (p["description"], p["code"]) not in seen]
            dupes = len(pairs) - len(new_unique)
            if dupes:
                logger.info("Deduplicated %d pairs", dupes)
            write_jsonl(output, existing + new_unique)
            logger.info("Written %d records → %s (total: %d)",
                        len(new_unique), output, len(existing) + len(new_unique))

        return len(pairs)


def _html_to_markdown(html: str, custom_tags: list[str] | None = None) -> str:
    """Convert HTML to a sequential markdown stream (text paragraphs + code fences).

    custom_tags: list of HTML tag names whose comment content is extracted as code.
    Uses BeautifulSoup if available for robustness, falls back to regex.
    """
    import html as html_mod  # noqa: PLC0415

    try:
        from bs4 import BeautifulSoup  # type: ignore  # noqa: PLC0415
        return _bs4_to_markdown(html, html_mod, custom_tags=custom_tags)
    except ImportError:
        pass

    return _regex_to_markdown(html, html_mod)


def _bs4_to_markdown(
    html: str,
    html_mod: object,  # type: ignore[type-arg]
    custom_tags: list[str] | None = None,
) -> str:
    """Convert HTML to markdown token stream.

    custom_tags: list of HTML tag names whose comment content is extracted as code.
    Example: ["java-playground"] for dev.java — avoids hardcoding site-specific tags.
    """
    from bs4 import BeautifulSoup, Comment  # type: ignore  # noqa: PLC0415

    custom_tag_set: set[str] = set(custom_tags or [])

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer",
                     "noscript", "aside", "table"]):
        tag.decompose()

    main = (soup.find(attrs={"role": "main"}) or
            soup.find("div", class_=re.compile(r"\bbody\b")) or
            soup.find("article") or
            soup.body or soup)

    tokens: list[tuple[str, str]] = []
    TEXT_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd"}
    _HIGHLIGHT_RE = re.compile(r"\bhighlight\b", re.IGNORECASE)
    _LANG_RE = re.compile(r"\blanguage-\w+\b", re.IGNORECASE)
    _SHIKI_RE = re.compile(r"\bshiki\b", re.IGNORECASE)

    for el in main.descendants:
        if not hasattr(el, "name") or el.name is None:
            continue

        # ── Shiki: <pre class="shiki ..." language="python"> ──────────────────
        if el.name == "pre" and _SHIKI_RE.search(" ".join(el.get("class", []))):
            if el.find_parent("pre", class_=_SHIKI_RE):
                continue
            code = el.get_text()
            if code.strip():
                tokens.append(("code", code.strip()))
            continue

        # ── highlight.js: <pre ...><code class="language-XXX hljs"> ──────────
        if el.name == "pre":
            code_el = el.find("code", class_=_LANG_RE)
            if code_el is not None:
                code = code_el.get_text()
                if code.strip():
                    tokens.append(("code", code.strip()))
                continue

        # ── Sphinx/Pygments: <div class="highlight*"> ─────────────────────────
        if el.name == "div" and _HIGHLIGHT_RE.search(" ".join(el.get("class", []))):
            if el.find_parent("div", class_=_HIGHLIGHT_RE):
                continue
            # Skip if already captured by hljs pre>code above
            if el.find("code", class_=_LANG_RE):
                continue
            code = el.get_text()
            if code.strip():
                tokens.append(("code", code.strip()))
            continue

        # ── Custom tags: code stored in HTML comments (e.g. java-playground) ──
        if el.name in custom_tag_set:
            code_parts: list[str] = []
            for child in el.descendants:
                if isinstance(child, Comment):
                    stripped = child.strip()
                    if stripped:
                        code_parts.append(stripped)
            code = "\n".join(code_parts)
            if code.strip():
                tokens.append(("code", code.strip()))
            continue

        # ── Text paragraphs / headings ────────────────────────────────────────
        if el.name in TEXT_TAGS:
            if el.find_parent("div", class_=_HIGHLIGHT_RE):
                continue
            if any(el.find_parent(t) for t in custom_tag_set):
                continue
            text = re.sub(r"\s+", " ", el.get_text()).strip()
            if text:
                tokens.append(("text", text))

    parts: list[str] = []
    for kind, content in tokens:
        parts.append(f"```\n{content}\n```" if kind == "code" else content)
    return "\n\n".join(parts)


def _regex_to_markdown(html: str, html_mod: object) -> str:  # type: ignore[type-arg]
    """Fallback regex-based extraction when BeautifulSoup is not available."""
    result: list[str] = []
    clean = re.sub(r"<(script|style|nav|header|footer)[^>]*>.*?</\1>", "", html,
                   flags=re.DOTALL | re.IGNORECASE)
    # Sphinx highlight divs: strip inner span tags, keep text
    for m in re.finditer(
        r'<div[^>]+class="[^"]*highlight[^"]*"[^>]*>.*?<pre[^>]*>(.*?)</pre>',
        clean, re.DOTALL | re.IGNORECASE
    ):
        code = re.sub(r"<[^>]+>", "", m.group(1))
        code = html_mod.unescape(code).strip()  # type: ignore[attr-defined]
        if code:
            result.append(f"```\n{code}\n```")
    for m in re.finditer(r"<(h[1-6]|p)[^>]*>(.*?)</\1>", clean, re.DOTALL | re.IGNORECASE):
        text = re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(2)))).strip()  # type: ignore[attr-defined]
        if text:
            result.append(text)
    return "\n\n".join(result)


def _extract_links(html: str) -> list[str]:
    return re.findall(r'<a\s[^>]*href=["\']([^"\'#][^"\']*)["\']', html, re.IGNORECASE)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect (description, code) records from a documentation site (neutral output)")
    parser.add_argument("--url",          required=True,  help="Seed URL to crawl")
    parser.add_argument("--lang",         required=True,  help="Language hint (python|java|typescript|go…)")
    parser.add_argument("--output",       required=True,  help="Output JSONL file")
    parser.add_argument("--max-pages",    type=int, default=200, dest="max_pages")
    parser.add_argument("--max-depth",    type=int, default=5,   dest="max_depth")
    parser.add_argument("--custom-tags",  nargs="*", default=[], dest="custom_tags",
                        help="HTML tag names whose comment content is extracted as code (e.g. java-playground)")
    parser.add_argument("--adapter-name", default=None, dest="adapter_name")
    parser.add_argument("--playwright",   action="store_true",
                        help="Use headless Chromium instead of httpx (bypasses JS/Cloudflare blocks)")
    args = parser.parse_args()

    collector = DocCollector(
        lang_hint=args.lang,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        custom_tags=args.custom_tags,
        use_playwright=args.playwright,
    )
    n = collector.run(seed_url=args.url, output=Path(args.output), adapter_name=args.adapter_name)
    print(f"Collected {n} records → {args.output}")


if __name__ == "__main__":
    main()
