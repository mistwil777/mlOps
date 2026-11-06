"""Content extraction from arbitrary file formats — multimodal.

Dispatch chain per extension:
  .java/.py/.txt/.md   → direct UTF-8 read
  .pdf                 → pdfplumber (text + tables) ; fallback to Claude Sonnet vision per page
  .docx                → python-docx paragraph join
  .xlsx                → openpyxl → markdown tables
  .pptx                → python-pptx slides text + images via vision
  .png/.jpg/.jpeg      → Claude Sonnet vision description
  .zip                 → recursive extraction of each member
  other                → chardet-assisted UTF-8 read

Vision LLM: Claude Sonnet via AWS Bedrock (us.anthropic.claude-sonnet-4-5).
Configurable via VISION_MODEL env var or passed explicitly.
"""

from __future__ import annotations

import base64
import io
import os
import uuid
import zipfile
from pathlib import Path

import logging

logger = logging.getLogger("pair_forge.extractors")

_TEXT_EXTENSIONS  = {".java", ".py", ".txt", ".md", ".ts", ".js", ".go", ".rs", ".c", ".cpp", ".h"}
_RTF_EXTENSIONS   = {".rtf"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_MIN_PDF_CHARS    = 50
_DEFAULT_VISION_MODEL = "us.anthropic.claude-sonnet-4-5"


def extract_content(path: Path, vision_model: str | None = None) -> tuple[str, str]:
    """Return (content, method_used) for any supported file type.

    method_used: 'text', 'pdf', 'pdf_multimodal', 'ocr', 'docx', 'xlsx', 'pptx',
                 'image', 'zip', 'fallback'.
    Raises ValueError if the file cannot be read at all.
    """
    suffix = path.suffix.lower()
    vm = vision_model or os.environ.get("MLOPS_VISION_MODEL", _DEFAULT_VISION_MODEL)

    if suffix in _TEXT_EXTENSIONS:
        return _read_text(path), "text"
    if suffix in _RTF_EXTENSIONS:
        return _read_rtf(path), "rtf"
    if suffix == ".pdf":
        return _read_pdf(path, vm)
    if suffix == ".docx":
        return _read_docx(path), "docx"
    if suffix == ".xlsx":
        return _read_xlsx(path), "xlsx"
    if suffix == ".pptx":
        return _read_pptx(path, vm), "pptx"
    if suffix in _IMAGE_EXTENSIONS:
        return _describe_image(path.read_bytes(), suffix.lstrip("."), vm), "image"
    if suffix == ".zip":
        return _read_zip(path, vm), "zip"
    return _read_fallback(path), "fallback"


# ── Vision helper ──────────────────────────────────────────────────────────────

def _describe_image(image_bytes: bytes, media_type_hint: str, vision_model: str) -> str:
    """Send image bytes to Claude Sonnet Bedrock, return structured description."""
    _type_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                 "webp": "image/webp", "gif": "image/gif"}
    media_type = _type_map.get(media_type_hint, "image/png")
    b64 = base64.standard_b64encode(image_bytes).decode()

    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        client = boto3.client(
            "bedrock-runtime", region_name="us-east-1",
            config=Config(connect_timeout=15, read_timeout=120, retries={"max_attempts": 2}),
        )
        response = client.converse(
            modelId=vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "image": {
                            "format": media_type.split("/")[1],
                            "source": {"bytes": image_bytes},
                        }
                    },
                    {
                        "text": (
                            "Describe this document page or image in detail. "
                            "Extract: all visible text (exact), table contents as markdown, "
                            "diagram descriptions, labels, numerical values, and their context. "
                            "Preserve structure. Output plain text."
                        )
                    },
                ],
            }],
            inferenceConfig={"maxTokens": 2048, "temperature": 0.0},
        )
        return response["output"]["message"]["content"][0]["text"].strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Vision LLM failed (%s): %s — falling back to empty description", vision_model, exc)
        return ""


# ── PDF ────────────────────────────────────────────────────────────────────────

def _read_pdf(path: Path, vision_model: str) -> tuple[str, str]:
    try:
        import pdfplumber  # noqa: PLC0415

        parts: list[str] = []
        pages_needing_vision: list[int] = []

        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                page_parts: list[str] = []

                # Text
                text = page.extract_text() or ""
                if text.strip():
                    page_parts.append(text.strip())

                # Tables → markdown
                tables = page.extract_tables() or []
                for table in tables:
                    if table:
                        md = _table_to_markdown(table)
                        if md:
                            page_parts.append(md)

                if page_parts:
                    parts.append("\n".join(page_parts))
                else:
                    pages_needing_vision.append(i)

        combined = "\n\n".join(parts).strip()

        # Vision for pages with no extractable text (scanned/image pages)
        if pages_needing_vision:
            try:
                from pdf2image import convert_from_path  # noqa: PLC0415

                images = convert_from_path(str(path))
                for i in pages_needing_vision:
                    if i < len(images):
                        buf = io.BytesIO()
                        images[i].save(buf, format="PNG")
                        desc = _describe_image(buf.getvalue(), "png", vision_model)
                        if desc:
                            parts.append(f"[Page {i+1} — vision]\n{desc}")
                combined = "\n\n".join(parts).strip()
                logger.info("PDF multimodal: %s (%d text pages, %d vision pages)",
                            path.name, len(pdf.pages) - len(pages_needing_vision), len(pages_needing_vision))
                return combined, "pdf_multimodal"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Vision fallback failed for %s: %s", path.name, exc)

        if len(combined) >= _MIN_PDF_CHARS:
            logger.info("PDF extracted via pdfplumber: %s (%d chars)", path.name, len(combined))
            return combined, "pdf"

        logger.info("PDF text too short (%d chars), trying vision: %s", len(combined), path.name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdfplumber failed on %s: %s — trying vision", path.name, exc)

    # Full vision fallback for entire PDF
    return _ocr_pdf_vision(path, vision_model)


def _ocr_pdf_vision(path: Path, vision_model: str) -> tuple[str, str]:
    """Convert PDF to images and describe each page with vision LLM."""
    try:
        from pdf2image import convert_from_path  # noqa: PLC0415

        images = convert_from_path(str(path))
        pages: list[str] = []
        for i, img in enumerate(images):
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            desc = _describe_image(buf.getvalue(), "png", vision_model)
            if desc:
                pages.append(f"[Page {i+1}]\n{desc}")
        text = "\n\n".join(pages).strip()
        logger.info("PDF vision: %s (%d chars, %d pages)", path.name, len(text), len(images))
        return text, "pdf_multimodal"
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF extraction failed on {path.name}: {exc}") from exc


def _table_to_markdown(table: list[list]) -> str:
    if not table:
        return ""
    rows = [[str(cell or "").strip() for cell in row] for row in table]
    if not rows:
        return ""
    header = "| " + " | ".join(rows[0]) + " |"
    separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = "\n".join("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join([header, separator, body]) if body else header


# ── XLSX ───────────────────────────────────────────────────────────────────────

def _read_xlsx(path: Path) -> str:
    try:
        import openpyxl  # noqa: PLC0415

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts: list[str] = []
        for sheet in wb.worksheets:
            rows = [[str(cell.value or "").strip() for cell in row] for row in sheet.iter_rows()]
            rows = [r for r in rows if any(c for c in r)]
            if rows:
                parts.append(f"## Sheet: {sheet.title}\n{_table_to_markdown(rows)}")
        return "\n\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"XLSX extraction failed on {path.name}: {exc}") from exc


# ── PPTX ──────────────────────────────────────────────────────────────────────

def _read_pptx(path: Path, vision_model: str) -> str:
    try:
        from pptx import Presentation  # noqa: PLC0415
        from pptx.util import Pt  # noqa: PLC0415

        prs = Presentation(str(path))
        parts: list[str] = []
        for i, slide in enumerate(prs.slides):
            slide_parts: list[str] = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            slide_parts.append(text)
                elif hasattr(shape, "image"):
                    img_bytes = shape.image.blob
                    ext = shape.image.ext.lower()
                    desc = _describe_image(img_bytes, ext, vision_model)
                    if desc:
                        slide_parts.append(f"[Image: {desc}]")
            if slide_parts:
                parts.append(f"## Slide {i+1}\n" + "\n".join(slide_parts))
        return "\n\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PPTX extraction failed on {path.name}: {exc}") from exc


# ── Standard formats ──────────────────────────────────────────────────────────

def _read_rtf(path: Path) -> str:
    try:
        from striprtf.striprtf import rtf_to_text  # noqa: PLC0415

        raw = path.read_text(encoding="utf-8", errors="replace")
        return rtf_to_text(raw).strip()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"RTF extraction failed on {path.name}: {exc}") from exc


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_docx(path: Path) -> str:
    try:
        from docx import Document  # noqa: PLC0415

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"DOCX extraction failed on {path.name}: {exc}") from exc


def _read_zip(path: Path, vision_model: str) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            member_path = Path(name)
            try:
                raw = zf.read(name)
                # Unique tmp path to avoid concurrent collisions
                tmp = Path(f"/tmp/_mlops_ingest_{uuid.uuid4().hex}_{member_path.name}")
                tmp.write_bytes(raw)
                content, method = extract_content(tmp, vision_model)
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
