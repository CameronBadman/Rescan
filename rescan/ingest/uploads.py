"""Document intake shared by the upload endpoint and bucket ingestion."""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# Archive members we will not unpack, and a ceiling on how much we will expand.
SKIP_PREFIXES = ("__MACOSX/", ".")
ALLOWED_SUFFIXES = {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md", ".html", ".htm",
                    ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
MAX_UNPACKED_BYTES = 512 * 1024 * 1024


def is_usable(name: str) -> bool:
    if any(name.startswith(prefix) for prefix in SKIP_PREFIXES) or name.endswith("/"):
        return False
    stem = Path(name).name
    if not stem or stem.startswith("."):
        return False
    return Path(name).suffix.lower() in ALLOWED_SUFFIXES


def expand_uploads(
    uploads: list[tuple[str, bytes]], *, max_bytes: int = MAX_UNPACKED_BYTES
) -> list[tuple[str, bytes]]:
    """Flatten zip archives into individual documents.

    Bulk uploads arrive as a zip of a folder as often as a multi-file
    selection, so both are accepted at the same endpoint.
    """
    expanded: list[tuple[str, bytes]] = []
    unpacked = 0

    for filename, data in uploads:
        if not filename.lower().endswith(".zip"):
            expanded.append((filename, data))
            continue
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            log.warning("skipping unreadable archive %s", filename)
            continue
        for info in archive.infolist():
            if info.is_dir() or not is_usable(info.filename):
                continue
            # Guard against a zip bomb rather than trusting the declared size.
            if unpacked + info.file_size > max_bytes:
                log.warning("archive %s exceeded the unpack limit; remaining members skipped", filename)
                break
            member = archive.read(info)
            unpacked += len(member)
            expanded.append((Path(info.filename).name, member))
    return expanded
