"""OCR fallback for scanned or image-only documents.

Tika returns almost nothing for a scanned PDF, so anything under the character
threshold is retried through Tesseract. When the OCR toolchain is not installed
the document is *not* dropped: the caller records a warning and routes it to the
manual-review bucket.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from rescan.config import settings

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


class OcrUnavailable(RuntimeError):
    """Raised when the OCR toolchain is missing from the host."""


def ocr_available() -> bool:
    return shutil.which(settings.tesseract_bin) is not None


def _pdf_rasteriser() -> str | None:
    for binary in ("pdftoppm", "pdftocairo"):
        if shutil.which(binary):
            return binary
    return None


def _run_tesseract(image_path: Path) -> str:
    proc = subprocess.run(
        [settings.tesseract_bin, str(image_path), "stdout", "-l", "eng"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        log.warning("tesseract failed on %s: %s", image_path.name, proc.stderr.strip()[:200])
        return ""
    return proc.stdout


def ocr_file(path: Path, max_pages: int = 10) -> str:
    """OCR an image or scanned PDF. Raises OcrUnavailable if tooling is absent."""
    if not ocr_available():
        raise OcrUnavailable(f"{settings.tesseract_bin} not found on PATH")

    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return _run_tesseract(path)

    if suffix != ".pdf":
        raise OcrUnavailable(f"no OCR path for {suffix or 'unknown'} files")

    rasteriser = _pdf_rasteriser()
    if rasteriser is None:
        raise OcrUnavailable("pdftoppm/pdftocairo not found; cannot rasterise PDF for OCR")

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        subprocess.run(
            [rasteriser, "-png", "-r", "200", "-l", str(max_pages), str(path), str(prefix)],
            capture_output=True,
            timeout=300,
            check=False,
        )
        pages = sorted(Path(tmp).glob("page*.png"))
        if not pages:
            return ""
        return "\n\n".join(_run_tesseract(page) for page in pages)
