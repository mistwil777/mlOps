"""Cleaning and normalization tools for raw ingested content.

Each tool is a pure function: str -> str.
The agent selects which tools to apply based on the extraction method.
"""

from __future__ import annotations

import re
import unicodedata


# ── Dispatch map: extraction method → ordered list of tools ───────────────────

TOOLS_BY_METHOD: dict[str, list[str]] = {
    "text":     ["normalize_encoding", "normalize_whitespace", "extract_code_blocks"],
    "rtf":      ["normalize_encoding", "normalize_whitespace", "strip_page_artifacts"],
    "pdf":      ["normalize_encoding", "normalize_whitespace", "strip_page_artifacts", "normalize_tables", "remove_boilerplate"],
    "ocr":      ["fix_ocr_noise", "normalize_encoding", "normalize_whitespace", "strip_page_artifacts", "normalize_tables"],
    "docx":     ["normalize_encoding", "normalize_whitespace", "remove_boilerplate", "normalize_tables"],
    "zip":      ["normalize_encoding", "normalize_whitespace"],
    "fallback": ["normalize_encoding", "normalize_whitespace"],
}


# ── Tool registry ──────────────────────────────────────────────────────────────

def apply_tools(content: str, method: str) -> tuple[str, list[str]]:
    """Apply the tool chain for the given extraction method.

    Returns (cleaned_content, list_of_tools_applied).
    """
    tool_names = TOOLS_BY_METHOD.get(method, TOOLS_BY_METHOD["fallback"])
    applied: list[str] = []
    for name in tool_names:
        fn = _REGISTRY.get(name)
        if fn is None:
            continue
        before = content
        content = fn(content)
        if content != before:
            applied.append(name)
    return content, applied


# ── Individual tools ───────────────────────────────────────────────────────────

def normalize_encoding(text: str) -> str:
    """Normalize unicode: typographic quotes, dashes, non-breaking spaces, etc."""
    replacements = {
        "‘": "'", "’": "'",   # curly single quotes
        "“": '"', "”": '"',   # curly double quotes
        "–": "-", "—": "-",   # en-dash, em-dash
        " ": " ", "​": "",    # non-breaking space, zero-width space
        "…": "...",                # ellipsis
        "﻿": "",                   # BOM
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    # Normalize to NFC, drop non-printable control chars except newline/tab
    text = unicodedata.normalize("NFC", text)
    text = "".join(c for c in text if unicodedata.category(c)[0] != "C" or c in "\n\t")
    return text


def normalize_whitespace(text: str) -> str:
    """Collapse multiple blank lines, strip trailing spaces, unify line endings."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    # Collapse 3+ consecutive blank lines into 2
    result, blanks = [], 0
    for line in lines:
        if line == "":
            blanks += 1
            if blanks <= 2:
                result.append(line)
        else:
            blanks = 0
            result.append(line)
    return "\n".join(result).strip()


def strip_page_artifacts(text: str) -> str:
    """Remove page numbers, repeated headers/footers, and form-feed characters."""
    # Remove form feeds
    text = text.replace("\x0c", "\n")
    # Remove standalone page numbers: lines that are only a number (possibly with "page"/"p.")
    text = re.sub(r"(?im)^\s*(page\s+)?\d+\s*$", "", text)
    # Remove lines that look like document headers repeated (same line 3+ times)
    lines = text.split("\n")
    line_counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 5:
            line_counts[stripped] = line_counts.get(stripped, 0) + 1
    repeated = {l for l, c in line_counts.items() if c >= 3}
    lines = [l for l in lines if l.strip() not in repeated]
    return normalize_whitespace("\n".join(lines))


def fix_ocr_noise(text: str) -> str:
    """Correct common OCR substitution errors and stray characters."""
    # Common OCR confusions in code contexts
    fixes = [
        (r"\bl\b(?=\d)", "1"),          # lone 'l' before digit → '1'
        (r"(?<=\d)O(?=\d)", "0"),        # 'O' between digits → '0'
        (r"(?<=[a-z])I(?=[a-z])", "l"),  # capital I inside word → lowercase l
        (r"[|](?=\s)", "I"),             # pipe at word boundary → I
        (r"\s{2,}", " "),               # multiple spaces → single (within line)
        (r"- \n(\w)", r"\1"),            # hyphenated line break → join word
    ]
    for pattern, replacement in fixes:
        text = re.sub(pattern, replacement, text)
    # Remove stray isolated special chars that OCR inserts (not in code context)
    text = re.sub(r"(?<!\S)[~`@#$%^&*]{1}(?!\S)", "", text)
    return text


def extract_code_blocks(text: str) -> str:
    """Annotate code blocks with explicit markers to help the LLM distinguish
    code from prose. Detects fenced blocks and indented blocks.
    """
    # Already fenced blocks: leave as-is
    if re.search(r"```", text):
        return text

    lines   = text.split("\n")
    result  = []
    in_code = False

    for line in lines:
        is_indented = line.startswith("    ") or line.startswith("\t")
        looks_like_code = is_indented and re.search(r"[;{}()=]", line)

        if looks_like_code and not in_code:
            result.append("```")
            in_code = True
        elif not looks_like_code and in_code:
            result.append("```")
            in_code = False
        result.append(line)

    if in_code:
        result.append("```")

    return "\n".join(result)


def remove_boilerplate(text: str) -> str:
    """Remove copyright notices, license headers, legal disclaimers, and TOC entries."""
    patterns = [
        r"(?im)^.*copyright\s+\d{4}.*$",
        r"(?im)^.*all rights reserved.*$",
        r"(?im)^.*licensed under.*$",
        r"(?im)^.*permission is hereby granted.*$",
        r"(?im)^.*this document is confidential.*$",
        r"(?im)^.*table of contents.*$",
        r"(?im)^\s*\d+\.\s{0,3}\.{5,}.*$",  # TOC entries like "1. ......... 12"
    ]
    for p in patterns:
        text = re.sub(p, "", text)
    return normalize_whitespace(text)


def normalize_tables(text: str) -> str:
    """Convert ASCII/unicode tables and tab-separated data into readable prose rows.

    Handles:
    - Markdown tables (|col1|col2|)
    - ASCII box tables (+---+---+)
    - Tab-separated values
    """
    lines  = text.split("\n")
    result = []
    i      = 0

    while i < len(lines):
        line = lines[i]

        # Markdown / pipe table
        if re.match(r"\s*\|", line) and "|" in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cells = [c for c in cells if c and not re.match(r"^[-:]+$", c)]
            if cells:
                result.append("Row: " + " | ".join(cells))
            i += 1
            continue

        # ASCII box table separator — skip
        if re.match(r"\s*\+[-=+]+\+", line):
            i += 1
            continue

        # Tab-separated row (at least 2 tabs)
        if line.count("\t") >= 2:
            cells = [c.strip() for c in line.split("\t") if c.strip()]
            if cells:
                result.append("Row: " + " | ".join(cells))
            i += 1
            continue

        result.append(line)
        i += 1

    return normalize_whitespace("\n".join(result))


def detect_language(text: str) -> str:
    """Return ISO 639-1 language code ('fr', 'en', 'de'…) or 'unknown'.

    Uses langdetect if available, falls back to a simple heuristic.
    """
    try:
        from langdetect import detect  # noqa: PLC0415
        return detect(text[:2000])
    except Exception:  # noqa: BLE001
        pass

    # Simple heuristic: count common French vs English stop words
    sample = text[:1000].lower()
    fr_words = sum(sample.count(w) for w in [" le ", " la ", " les ", " de ", " du ", " est ", " que "])
    en_words = sum(sample.count(w) for w in [" the ", " is ", " are ", " of ", " and ", " in ", " to "])
    if fr_words > en_words:
        return "fr"
    if en_words > 0:
        return "en"
    return "unknown"


# ── Dependency manifest ───────────────────────────────────────────────────────
# Maps each capability to its Python package(s) and optional system binary.
# Used by check_dependencies() so any developer can onboard without reading source.

_DEPENDENCY_MANIFEST: list[dict] = [
    {
        "capability": "RTF extraction",
        "trigger":    "files with .rtf extension",
        "pip":        ["striprtf"],
        "system":     [],
        "import":     "striprtf.striprtf",
    },
    {
        "capability": "PDF text extraction",
        "trigger":    "files with .pdf extension",
        "pip":        ["pdfplumber"],
        "system":     [],
        "import":     "pdfplumber",
    },
    {
        "capability": "PDF OCR (scanned documents)",
        "trigger":    "PDF files with no selectable text",
        "pip":        ["pytesseract", "pdf2image"],
        "system":     ["tesseract"],  # brew install tesseract / apt-get install tesseract-ocr
        "import":     "pytesseract",
    },
    {
        "capability": "Word document extraction",
        "trigger":    "files with .docx extension",
        "pip":        ["python-docx"],
        "system":     [],
        "import":     "docx",
    },
    {
        "capability": "Encoding detection (unknown formats)",
        "trigger":    "files with unrecognized extension",
        "pip":        ["chardet"],
        "system":     [],
        "import":     "chardet",
    },
    {
        "capability": "Language detection",
        "trigger":    "all files — used to adapt generation prompt",
        "pip":        ["langdetect"],
        "system":     [],
        "import":     "langdetect",
    },
]


def check_dependencies() -> list[dict]:
    """Check all agent dependencies. Returns a list of missing items.

    Each missing item has keys: capability, pip, system, install_hint.
    Returns an empty list if everything is available.

    Usage:
        missing = check_dependencies()
        for m in missing:
            print(f"[MISSING] {m['capability']}: pip install {' '.join(m['pip'])}")
    """
    import importlib  # noqa: PLC0415
    import shutil     # noqa: PLC0415

    missing = []
    for dep in _DEPENDENCY_MANIFEST:
        pip_missing    = [p for p in dep["pip"]    if not _can_import(dep["import"])]
        system_missing = [b for b in dep["system"] if shutil.which(b) is None]

        if pip_missing or system_missing:
            hint_parts = []
            if pip_missing:
                hint_parts.append(f"pip install {' '.join(dep['pip'])}")
            if system_missing:
                hint_parts.append(
                    f"brew install {' '.join(system_missing)}"
                    f"  # or: apt-get install {' '.join(_apt_names(system_missing))}"
                )
            missing.append({
                "capability":   dep["capability"],
                "trigger":      dep["trigger"],
                "pip":          pip_missing,
                "system":       system_missing,
                "install_hint": " && ".join(hint_parts),
            })

    return missing


def _can_import(module: str) -> bool:
    import importlib  # noqa: PLC0415
    try:
        importlib.import_module(module.split(".")[0])
        return True
    except ImportError:
        return False


def _apt_names(brew_names: list[str]) -> list[str]:
    mapping = {"tesseract": "tesseract-ocr"}
    return [mapping.get(n, n) for n in brew_names]


# ── Registry ───────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, object] = {
    "normalize_encoding":   normalize_encoding,
    "normalize_whitespace": normalize_whitespace,
    "strip_page_artifacts": strip_page_artifacts,
    "fix_ocr_noise":        fix_ocr_noise,
    "extract_code_blocks":  extract_code_blocks,
    "remove_boilerplate":   remove_boilerplate,
    "normalize_tables":     normalize_tables,
    "detect_language":      detect_language,
}
