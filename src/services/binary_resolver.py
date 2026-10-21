"""Tiered binary resolution engine for ffprobe, ffmpeg, and tesseract.

Resolution Hierarchy:
  1. User-configured manual path (if file exists and is executable)
  2. System PATH discovery via shutil.which
  3. Standard vendor / common filesystem locations (Jellyfin, Chocolatey, Program Files, Linux /usr)
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

COMMON_PATHS: dict[str, list[str]] = {
    "ffprobe": [
        r"C:\Program Files\Jellyfin\Server\ffprobe.exe",
        r"C:\Program Files (x86)\Jellyfin\Server\ffprobe.exe",
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\ProgramData\chocolatey\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        "/usr/bin/ffprobe",
        "/usr/local/bin/ffprobe",
        "/usr/lib/jellyfin-ffmpeg/ffprobe",
    ],
    "ffmpeg": [
        r"C:\Program Files\Jellyfin\Server\ffmpeg.exe",
        r"C:\Program Files (x86)\Jellyfin\Server\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/lib/jellyfin-ffmpeg/ffmpeg",
    ],
    "tesseract": [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        r"C:\Users\%USERNAME%\AppData\Local\Programs\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ],
}


def _is_executable(path_str: str) -> bool:
    """Check if ``path_str`` points to an existing file that can be executed."""
    try:
        path = Path(path_str)
        if not path.is_file():
            return False
        if sys.platform == "win32":
            # On Windows, os.X_OK is often set for all files; check for executable extensions
            ext = path.suffix.lower()
            return ext in {".exe", ".cmd", ".bat", ".com"} or os.access(str(path), os.X_OK)
        return os.access(str(path), os.X_OK)
    except (OSError, ValueError):
        return False


def resolve_binary(binary_name: str, custom_path: str | None = None) -> Path | None:
    """Resolve executable path using tiered priority.

    Priority 1: User-specified manual path (if file exists and is executable)
    Priority 2: System PATH discovery via shutil.which
    Priority 3: Standard known installation directories (Jellyfin, Program Files, Linux /usr)
    """
    clean_name = binary_name.strip().lower()
    if clean_name.endswith(".exe"):
        clean_name = clean_name[:-4]

    # Priority 1: Valid manual override
    if custom_path and custom_path.strip():
        expanded = os.path.expandvars(custom_path.strip())
        if _is_executable(expanded):
            resolved = Path(expanded).resolve()
            logger.debug("Resolved %s via manual override: %s", binary_name, resolved)
            return resolved
        logger.warning(
            "Configured custom %s path %s does not exist or is not executable",
            binary_name,
            custom_path,
        )

    # Priority 2: System PATH via shutil.which
    in_path = shutil.which(clean_name)
    if in_path and _is_executable(in_path):
        resolved = Path(in_path).resolve()
        logger.debug("Resolved %s via system PATH: %s", binary_name, resolved)
        return resolved

    # Priority 3: Standard vendor / common filesystem locations
    for candidate in COMMON_PATHS.get(clean_name, []):
        expanded = os.path.expandvars(candidate)
        if _is_executable(expanded):
            resolved = Path(expanded).resolve()
            logger.info("Auto-detected %s in standard location: %s", binary_name, resolved)
            return resolved

    logger.debug("Unable to resolve executable for %s across all tiers", binary_name)
    return None
