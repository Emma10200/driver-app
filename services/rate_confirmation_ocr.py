"""Free OCR fallback for scanned/image-only rate-confirmation PDFs.

Uses pdf2image (Poppler) to render PDF pages into Pillow images and
pytesseract (Tesseract OCR engine) to extract text. Imports are intentionally
lazy so the app can still run in environments where OCR binaries are not yet
installed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_OCR_DPI = 200
DEFAULT_OCR_MAX_PAGES = 4
DEFAULT_OCR_MAX_CHARS = 20000
DEFAULT_PDF_RENDER_TIMEOUT_SECONDS = 60
DEFAULT_TESSERACT_TIMEOUT_SECONDS = 30
DEFAULT_TESSERACT_CONFIG = "--oem 3 --psm 6"


@dataclass(frozen=True)
class OcrResult:
    text: str = ""
    status: str = "not_started"
    pages_rendered: int = 0
    pages_ocrd: int = 0
    chars: int = 0
    error: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "pages_rendered": self.pages_rendered,
            "pages_ocrd": self.pages_ocrd,
            "chars": self.chars,
            "error": self.error[:300],
        }


def _import_pdf2image() -> tuple[Callable[..., list[Any]], tuple[type[BaseException], ...]]:
    from pdf2image import convert_from_bytes
    from pdf2image.exceptions import (
        PDFInfoNotInstalledError,
        PDFPageCountError,
        PDFPopplerTimeoutError,
        PDFSyntaxError,
    )

    return convert_from_bytes, (
        PDFInfoNotInstalledError,
        PDFPageCountError,
        PDFPopplerTimeoutError,
        PDFSyntaxError,
    )


def _import_pytesseract() -> Any:
    import pytesseract

    return pytesseract


def ocr_pdf_text(
    payload: bytes,
    *,
    max_pages: int = DEFAULT_OCR_MAX_PAGES,
    dpi: int = DEFAULT_OCR_DPI,
    max_chars: int = DEFAULT_OCR_MAX_CHARS,
    lang: str = "eng",
    tesseract_config: str = DEFAULT_TESSERACT_CONFIG,
    poppler_path: str = "",
    tesseract_cmd: str = "",
    pdf_timeout: int = DEFAULT_PDF_RENDER_TIMEOUT_SECONDS,
    tesseract_timeout: int = DEFAULT_TESSERACT_TIMEOUT_SECONDS,
) -> OcrResult:
    """OCR a PDF byte payload using the free Tesseract + Poppler toolchain.

    Returns an ``OcrResult`` instead of raising for missing dependencies/binaries
    or bad PDFs, because OCR is a second-layer enhancement and should not break
    the read-only ingest pipeline.
    """
    if not payload:
        return OcrResult(status="empty_payload")

    max_pages = max(1, int(max_pages or DEFAULT_OCR_MAX_PAGES))
    dpi = max(100, int(dpi or DEFAULT_OCR_DPI))
    max_chars = max(1000, int(max_chars or DEFAULT_OCR_MAX_CHARS))

    try:
        convert_from_bytes, pdf_exceptions = _import_pdf2image()
    except Exception as exc:  # pragma: no cover - depends on optional package install
        logger.warning("pdf2image is unavailable for rate-confirmation OCR: %s", exc)
        return OcrResult(status="ocr_unavailable", error=f"pdf2image unavailable: {exc}")

    try:
        pytesseract = _import_pytesseract()
    except Exception as exc:  # pragma: no cover - depends on optional package install
        logger.warning("pytesseract is unavailable for rate-confirmation OCR: %s", exc)
        return OcrResult(status="ocr_unavailable", error=f"pytesseract unavailable: {exc}")

    if tesseract_cmd:
        try:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        except Exception:
            pass

    try:
        images = convert_from_bytes(
            payload,
            dpi=dpi,
            first_page=1,
            last_page=max_pages,
            thread_count=1,
            grayscale=True,
            poppler_path=poppler_path or None,
            timeout=pdf_timeout,
        )
    except pdf_exceptions as exc:
        logger.warning("Could not render PDF for OCR: %s", exc)
        return OcrResult(status="render_failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - optional binary/runtime errors vary by platform
        logger.warning("Unexpected PDF render failure for OCR: %s", exc)
        return OcrResult(status="render_failed", error=str(exc))

    chunks: list[str] = []
    pages_ocrd = 0
    for image in images[:max_pages]:
        try:
            text = pytesseract.image_to_string(
                image,
                lang=lang or "eng",
                config=tesseract_config or DEFAULT_TESSERACT_CONFIG,
                timeout=tesseract_timeout,
            )
        except RuntimeError as exc:
            logger.warning("Tesseract OCR timed out/failed on a page: %s", exc)
            return OcrResult(
                text="\n".join(chunks)[:max_chars],
                status="ocr_partial" if chunks else "ocr_failed",
                pages_rendered=len(images),
                pages_ocrd=pages_ocrd,
                chars=len("\n".join(chunks)[:max_chars]),
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - TesseractNotFoundError is version-specific
            logger.warning("Tesseract OCR failed: %s", exc)
            return OcrResult(
                text="\n".join(chunks)[:max_chars],
                status="ocr_partial" if chunks else "ocr_failed",
                pages_rendered=len(images),
                pages_ocrd=pages_ocrd,
                chars=len("\n".join(chunks)[:max_chars]),
                error=str(exc),
            )
        pages_ocrd += 1
        if text.strip():
            chunks.append(text)
        joined = "\n".join(chunks)
        if len(joined) >= max_chars:
            joined = joined[:max_chars]
            return OcrResult(
                text=joined,
                status="ocr_text_extracted",
                pages_rendered=len(images),
                pages_ocrd=pages_ocrd,
                chars=len(joined),
            )

    joined = "\n".join(chunks)[:max_chars]
    return OcrResult(
        text=joined,
        status="ocr_text_extracted" if joined.strip() else "ocr_no_text",
        pages_rendered=len(images),
        pages_ocrd=pages_ocrd,
        chars=len(joined),
    )
