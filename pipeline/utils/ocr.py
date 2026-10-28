"""OCR utilities — extract text from PDF/image files with a confidence score.

Confidence score rationale (inspired by ORANO MELOX POC):
    A raw OCR word count or char count tells you nothing about legibility.
    We estimate confidence from two signals:
    1. Character quality ratio — proportion of printable, non-garbage characters
    2. Word density — average word length (garbled OCR produces very short or very long tokens)
    Score in [0, 1]; records below OCR_CONFIDENCE_MIN should be excluded from training.

Dependency strategy (all optional — graceful degradation):
    - pdfplumber  → text-layer PDF extraction (fast, no OCR needed for digital PDFs)
    - pytesseract → Tesseract OCR for scanned PDFs and images (requires tesseract binary)
    - Pillow      → image loading for pytesseract
    - pdf2image   → PDF page rendering for pytesseract (requires poppler)
    If none are available, the record is returned with confidence=0 and a warning.

Usage:
    from pipeline.utils.ocr import ocr_file, ocr_records, OCR_CONFIDENCE_MIN

    result = ocr_file(Path("scan.pdf"))
    print(result.text[:200], result.confidence, result.method)

    enriched = ocr_records(raw_records)  # adds _ocr_text, _ocr_confidence, _ocr_method
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.ocr")

OCR_CONFIDENCE_MIN = 0.60  # below this → exclude from pipeline

_PRINTABLE = set(string.printable)
_SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
_SUPPORTED_PDF_SUFFIX = ".pdf"


@dataclass
class OcrResult:
    text: str
    confidence: float       # 0–1
    method: str             # "pdfplumber" | "tesseract" | "unavailable"
    n_chars: int
    n_words: int
    warning: str | None = None


# ── Confidence estimation ─────────────────────────────────────────────────────

def _estimate_confidence(text: str) -> float:
    """Estimate OCR quality from three independent signals.

    Returns 0 for empty text.
    - char_quality  : fraction of printable chars (garbled scans have control chars)
    - alpha_ratio   : fraction of alpha chars among non-space chars — real text (prose
                      or code) is mostly letters; pure noise mixes symbols, digits, garbage
    - word_density  : fraction of "normal" words (3–20 chars) — garbled OCR skews extremes

    Threshold: OCR_CONFIDENCE_MIN = 0.60 (below → exclude record from training).
    """
    if not text or not text.strip():
        return 0.0

    chars = list(text)
    n_chars = len(chars)

    # Signal 1 — printable chars
    n_printable = sum(1 for c in chars if c in _PRINTABLE)
    char_quality = n_printable / n_chars if n_chars else 0.0

    # Signal 2 — alpha ratio (prose/code is mostly letters; random OCR noise is not)
    non_space = [c for c in chars if not c.isspace()]
    n_non_space = len(non_space)
    n_alpha = sum(1 for c in non_space if c.isalpha())
    alpha_ratio = n_alpha / n_non_space if n_non_space else 0.0

    # Signal 3 — word density (normal-length tokens)
    words = text.split()
    n_words = len(words)
    if n_words == 0:
        return round(char_quality * 0.3, 3)
    normal_words = sum(1 for w in words if 3 <= len(w) <= 20)
    word_density = normal_words / n_words

    # Weighted combination — alpha_ratio is the strongest discriminator for OCR quality
    score = 0.25 * char_quality + 0.50 * alpha_ratio + 0.25 * word_density
    return round(min(1.0, max(0.0, score)), 3)


# ── Extraction backends ───────────────────────────────────────────────────────

def _extract_pdf_pdfplumber(path: Path) -> tuple[str, str]:
    """Extract text from a digital (text-layer) PDF using pdfplumber."""
    import pdfplumber  # noqa: PLC0415

    pages_text: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                pages_text.append(t.strip())
    return "\n\n".join(pages_text), "pdfplumber"


def _extract_pdf_tesseract(path: Path) -> tuple[str, str]:
    """Render PDF pages as images and run Tesseract OCR (for scanned PDFs)."""
    from pdf2image import convert_from_path  # noqa: PLC0415
    import pytesseract  # noqa: PLC0415

    images = convert_from_path(str(path), dpi=300)
    pages_text: list[str] = []
    for img in images:
        t = pytesseract.image_to_string(img, lang="fra+eng")
        if t.strip():
            pages_text.append(t.strip())
    return "\n\n".join(pages_text), "tesseract"


def _extract_image_tesseract(path: Path) -> tuple[str, str]:
    """Run Tesseract OCR on an image file."""
    from PIL import Image  # noqa: PLC0415
    import pytesseract  # noqa: PLC0415

    img = Image.open(str(path))
    text = pytesseract.image_to_string(img, lang="fra+eng")
    return text.strip(), "tesseract"


# ── Public API ────────────────────────────────────────────────────────────────

def ocr_file(path: Path) -> OcrResult:
    """Extract text from a PDF or image file and return an OcrResult.

    Strategy for PDFs:
        1. Try pdfplumber (text-layer, fast, high quality for digital PDFs)
        2. If result is empty or low-confidence, fall back to Tesseract
    Strategy for images:
        1. Tesseract only
    """
    suffix = path.suffix.lower()

    if suffix == _SUPPORTED_PDF_SUFFIX:
        # Try text-layer first
        try:
            text, method = _extract_pdf_pdfplumber(path)
            conf = _estimate_confidence(text)
            if conf >= OCR_CONFIDENCE_MIN or _tesseract_unavailable():
                words = text.split()
                return OcrResult(
                    text=text, confidence=conf, method=method,
                    n_chars=len(text), n_words=len(words),
                )
            # Text layer confidence too low → this is a scanned PDF, try Tesseract
            logger.info("pdfplumber confidence %.2f < %.2f for %s — trying Tesseract", conf, OCR_CONFIDENCE_MIN, path.name)
        except ImportError:
            logger.warning("pdfplumber not installed — trying Tesseract for %s", path.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdfplumber failed on %s: %s — trying Tesseract", path.name, exc)

        try:
            text, method = _extract_pdf_tesseract(path)
            conf = _estimate_confidence(text)
            words = text.split()
            return OcrResult(text=text, confidence=conf, method=method, n_chars=len(text), n_words=len(words))
        except ImportError:
            return OcrResult(
                text="", confidence=0.0, method="unavailable",
                n_chars=0, n_words=0,
                warning="Neither pdfplumber nor pytesseract+pdf2image are installed. "
                        "Run: pip install pdfplumber pytesseract pdf2image",
            )
        except Exception as exc:  # noqa: BLE001
            return OcrResult(text="", confidence=0.0, method="error", n_chars=0, n_words=0, warning=str(exc))

    elif suffix in _SUPPORTED_IMAGE_SUFFIXES:
        try:
            text, method = _extract_image_tesseract(path)
            conf = _estimate_confidence(text)
            words = text.split()
            return OcrResult(text=text, confidence=conf, method=method, n_chars=len(text), n_words=len(words))
        except ImportError:
            return OcrResult(
                text="", confidence=0.0, method="unavailable",
                n_chars=0, n_words=0,
                warning="pytesseract or Pillow not installed. Run: pip install pytesseract Pillow",
            )
        except Exception as exc:  # noqa: BLE001
            return OcrResult(text="", confidence=0.0, method="error", n_chars=0, n_words=0, warning=str(exc))

    else:
        return OcrResult(
            text="", confidence=0.0, method="unsupported",
            n_chars=0, n_words=0,
            warning=f"Unsupported file type: {suffix}. Supported: pdf, png, jpg, jpeg, tiff, bmp",
        )


def ocr_records(
    records: list[dict[str, Any]],
    min_confidence: float = OCR_CONFIDENCE_MIN,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Enrich records that have a `file_path` field with OCR-extracted text.

    Returns (kept, excluded):
    - kept:     records with confidence >= min_confidence (or records without file_path)
    - excluded: records whose OCR confidence is too low

    For records that already have text content, this is a no-op.
    The `file_path` field is expected to point to a PDF or image on disk.
    """
    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for r in records:
        file_path = r.get("file_path")
        if not file_path:
            kept.append(r)
            continue

        path = Path(str(file_path))
        if not path.exists():
            logger.warning("OCR: file not found: %s — skipping record", path)
            excluded.append({**r, "_ocr_confidence": 0.0, "_ocr_method": "file_not_found"})
            continue

        result = ocr_file(path)

        if result.warning:
            logger.warning("OCR warning for %s: %s", path.name, result.warning)

        enriched = {
            **r,
            "_ocr_text":       result.text,
            "_ocr_confidence": result.confidence,
            "_ocr_method":     result.method,
            "_ocr_n_chars":    result.n_chars,
            "_ocr_n_words":    result.n_words,
        }

        # Promote OCR text into the record's primary text field if empty
        if result.text and not r.get("text") and not r.get("content") and not r.get("code"):
            enriched["text"] = result.text

        if result.confidence >= min_confidence:
            kept.append(enriched)
        else:
            reason = (
                f"OCR confidence {result.confidence:.2f} < {min_confidence:.2f}"
                if result.confidence > 0
                else (result.warning or "OCR failed")
            )
            logger.info("OCR excluded %s: %s", path.name, reason)
            excluded.append({**enriched, "_ocr_excluded_reason": reason})

    if excluded:
        logger.info(
            "OCR preprocessing: %d/%d records excluded (confidence < %.2f)",
            len(excluded), len(records), min_confidence,
        )

    return kept, excluded


def _tesseract_unavailable() -> bool:
    try:
        import pytesseract  # noqa: PLC0415
        pytesseract.get_tesseract_version()
        return False
    except Exception:
        return True
