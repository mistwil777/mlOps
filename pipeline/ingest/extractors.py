"""Content extraction from arbitrary file formats.

Dispatch chain per extension:
  .java/.py/.txt/.md   → direct UTF-8 read
  .pdf                 → pdfplumber text ; fallback to pytesseract OCR if result < 50 chars
  .docx                → python-docx paragraph join
  .zip                 → recursive extraction of each member
  other                → chardet-assisted UTF-8 read
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.ingest.extractors")

_TEXT_EXTENSIONS = {".java", ".py", ".txt", ".md", ".ts", ".js", ".go", ".rs", ".c", ".cpp", ".h"}
_RTF_EXTENSIONS  = {".rtf"}
_MIN_PDF_CHARS = 50


def extract_content(path: Path) -> tuple[str, str]:
    """Return (content, method_used) for any supported file type.

    method_used is one of: 'text', 'pdf', 'ocr', 'docx', 'zip', 'fallback'.
    Raises ValueError if the file cannot be read at all.
    """
    suffix = path.suffix.lower()

    if suffix in _TEXT_EXTENSIONS:
        return _read_text(path), "text"

    if suffix in _RTF_EXTENSIONS:
        return _read_rtf(path), "rtf"

    if suffix == ".pdf":
        return _read_pdf(path)

    if suffix == ".docx":
        return _read_docx(path), "docx"

    if suffix == ".zip":
        return _read_zip(path), "zip"

    return _read_fallback(path), "fallback"


def _read_rtf(path: Path) -> str:
    try:
        from striprtf.striprtf import rtf_to_text  # noqa: PLC0415

        raw = path.read_text(encoding="utf-8", errors="replace")
        return rtf_to_text(raw).strip()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"RTF extraction failed on {path.name}: {exc}") from exc


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> tuple[str, str]:
    try:
        import pdfplumber  # noqa: PLC0415

        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages).strip()
        if len(text) >= _MIN_PDF_CHARS:
            logger.info("PDF extracted via pdfplumber: %s (%d chars)", path.name, len(text))
            return text, "pdf"
        logger.info("PDF text too short (%d chars), switching to OCR: %s", len(text), path.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdfplumber failed on %s: %s — switching to OCR", path.name, exc)

    return _ocr_pdf(path), "ocr"


def _ocr_pdf(path: Path) -> str:
    try:
        import pytesseract  # noqa: PLC0415
        from pdf2image import convert_from_path  # noqa: PLC0415

        images = convert_from_path(str(path))
        pages = [pytesseract.image_to_string(img) for img in images]
        text = "\n".join(pages).strip()
        logger.info("OCR completed: %s (%d chars, %d pages)", path.name, len(text), len(images))
        return text
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"OCR failed on {path.name}: {exc}") from exc


def _read_docx(path: Path) -> str:
    try:
        from docx import Document  # noqa: PLC0415

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"DOCX extraction failed on {path.name}: {exc}") from exc


def _read_zip(path: Path) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            member_path = Path(name)
            try:
                raw = zf.read(name)
                tmp = Path(f"/tmp/_mlops_ingest_{member_path.name}")
                tmp.write_bytes(raw)
                content, method = extract_content(tmp)
                tmp.unlink(missing_ok=True)
                parts.append(f"# --- {name} ({method}) ---\n{content}")
                logger.info("ZIP member extracted: %s via %s", name, method)
            except Exception as exc:  # noqa: BLE001
                logger.warning("ZIP member skipped %s: %s", name, exc)
    return "\n\n".join(parts)


def _read_fallback(path: Path) -> str:
    try:
        import chardet  # noqa: PLC0415

        raw = path.read_bytes()
        detected = chardet.detect(raw)
        encoding = detected.get("encoding") or "utf-8"
        return raw.decode(encoding, errors="replace")
    except Exception:  # noqa: BLE001
        return path.read_text(encoding="utf-8", errors="replace")
