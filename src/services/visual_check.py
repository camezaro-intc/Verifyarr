"""Tier 3 visual checks: ffmpeg ``freezedetect`` and pytesseract OCR of opening/ending credits."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.services.prober import ToolError, find_binary, run_command

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------------
# Freeze-frame / loop detection
# ---------------------------------------------------------------------------------------

_FREEZE_LINE = re.compile(
    r"lavfi\.freezedetect\.freeze_(start|duration|end):\s*([0-9]+(?:\.[0-9]+)?)"
)


@dataclass(slots=True)
class FreezeResult:
    sample_seconds: float
    # (start offset within the sample, duration) in seconds
    intervals: list[tuple[float, float]] = field(default_factory=list)

    @property
    def frozen_seconds(self) -> float:
        return sum(duration for _, duration in self.intervals)

    @property
    def frozen_ratio(self) -> float:
        return (
            min(1.0, self.frozen_seconds / self.sample_seconds) if self.sample_seconds > 0 else 0.0
        )


def parse_freezedetect(stderr: str, sample_seconds: float) -> FreezeResult:
    """Parse ffmpeg's ``lavfi.freezedetect.*`` log lines into freeze intervals.

    A freeze that is still running when the sample ends (the classic looped-still trap)
    produces a ``freeze_start`` with no matching ``freeze_duration``; it is counted up to the
    end of the sample.
    """
    result = FreezeResult(sample_seconds=sample_seconds)
    open_start: float | None = None
    for kind, raw in _FREEZE_LINE.findall(stderr):
        value = float(raw)
        if kind == "start":
            open_start = value
        elif kind == "duration":
            result.intervals.append((open_start if open_start is not None else 0.0, value))
            open_start = None
    if open_start is not None:
        result.intervals.append((open_start, max(0.0, sample_seconds - open_start)))
    return result


async def detect_freeze_frames(
    file_path: Path,
    start_time: int = 600,
    duration: int = 180,
    *,
    min_freeze_seconds: float = 5,
    ffmpeg: Path | str | None = None,
    timeout: float = 300,
) -> FreezeResult:
    """Run ``freezedetect`` over ``[start_time, start_time + duration)`` and report frozen time."""
    binary = find_binary("ffmpeg", ffmpeg)
    result = await run_command(
        [
            binary,
            "-hide_banner",
            "-nostats",
            "-ss",
            str(start_time),
            "-t",
            str(duration),
            "-i",
            file_path,
            "-an",
            "-sn",
            "-vf",
            f"freezedetect=n=-60dB:d={min_freeze_seconds:g}",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ToolError(
            f"ffmpeg freezedetect failed (exit {result.returncode}): {result.stderr.strip()[-300:]}"
        )
    return parse_freezedetect(result.stderr, sample_seconds=float(duration))


# ---------------------------------------------------------------------------------------
# OCR of opening / ending credits
# ---------------------------------------------------------------------------------------


async def _extract_frame_png(
    file_path: Path, timestamp: int, ffmpeg: Path, timeout: float
) -> bytes:
    """Pipe a single frame into memory as PNG (``image2pipe``); empty bytes if past EOF."""
    result = await run_command(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(timestamp),
            "-i",
            file_path,
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ToolError(
            f"ffmpeg frame extraction failed at {timestamp}s: {result.stderr.strip()[-200:]}"
        )
    return result.stdout


def _ocr_image(png_bytes: bytes, tesseract_cmd: str | None) -> str:
    """Blocking OCR; always called through ``asyncio.to_thread``."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise ToolError(
            f"OCR dependencies missing ({exc}); install pytesseract and pillow"
        ) from exc

    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            # Credits are usually light text on dark: grayscale + autocontrast helps Tesseract.
            prepared = ImageOps.autocontrast(image.convert("L"))
            return pytesseract.image_to_string(prepared, timeout=60)
    except pytesseract.TesseractNotFoundError as exc:
        raise ToolError(
            "tesseract binary not found; install it or set binaries.tesseract_path"
        ) from exc
    except (RuntimeError, OSError) as exc:
        raise ToolError(f"OCR failed: {exc}") from exc


async def ocr_scan_frames(
    file_path: Path,
    timestamps: list[int],
    *,
    ffmpeg: Path | str | None = None,
    tesseract: Path | str | None = None,
    timeout: float = 60,
) -> list[str]:
    """Extract a frame at each timestamp and OCR it. Returns one text blob per timestamp."""
    ffmpeg_bin = find_binary("ffmpeg", ffmpeg)
    texts: list[str] = []
    for timestamp in timestamps:
        png = await _extract_frame_png(file_path, timestamp, ffmpeg_bin, timeout)
        if not png:
            logger.debug("No frame at %ss in %s", timestamp, file_path.name)
            texts.append("")
            continue
        texts.append(
            await asyncio.to_thread(_ocr_image, png, str(tesseract) if tesseract else None)
        )
    return texts


def find_keywords(texts: list[str], keywords: list[str]) -> list[str]:
    """Return the blacklisted keywords found in OCR output (case/whitespace-insensitive)."""
    haystack = " ".join(" ".join(texts).split()).casefold()
    return [kw for kw in keywords if kw.strip() and " ".join(kw.split()).casefold() in haystack]
