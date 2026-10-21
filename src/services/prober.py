"""ffprobe execution and audio-stream analysis (Tier 1 data source and Tier 2 logic)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """An external binary (ffprobe / ffmpeg / tesseract) failed or is unavailable."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: str


async def run_command(args: Sequence[str | Path], timeout: float = 120) -> CommandResult:
    """Run a subprocess without blocking the event loop; kill it if it exceeds ``timeout``."""
    str_args = [str(a) for a in args]
    logger.debug("Running: %s", " ".join(str_args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *str_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise ToolError(f"Cannot execute {str_args[0]}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ToolError(f"{Path(str_args[0]).name} timed out after {timeout:.0f}s") from exc
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    return CommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr.decode("utf-8", errors="replace"),
    )


def find_binary(name: str, configured: Path | str | None = None) -> Path:
    """Return ``configured`` if given, otherwise locate ``name`` on PATH via ``shutil.which``."""
    if configured:
        return Path(configured)
    found = shutil.which(name)
    if not found:
        raise ToolError(f"{name} was not found on PATH; install it or set binaries.{name}_path")
    return Path(found)


# ---------------------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioStream:
    codec: str | None
    channels: int | None
    language: str | None  # ISO 639-2 tag as reported by the container, lower-cased


@dataclass(slots=True)
class ProbeResult:
    duration_seconds: float
    audio_streams: list[AudioStream] = field(default_factory=list)
    has_video: bool = True

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60

    @property
    def audio_codecs(self) -> list[str]:
        return [s.codec for s in self.audio_streams if s.codec]

    @property
    def audio_channels(self) -> list[int]:
        return [s.channels for s in self.audio_streams if s.channels]

    @property
    def languages(self) -> list[str]:
        return [s.language for s in self.audio_streams if s.language]


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def probe_file(
    file_path: Path, ffprobe: Path | str | None = None, timeout: float = 120
) -> ProbeResult:
    """Run ffprobe and return container runtime plus audio codec / channel / language info."""
    binary = find_binary("ffprobe", ffprobe)
    result = await run_command(
        [
            binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,channels:stream_tags=language",
            "-of",
            "json",
            file_path,
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ToolError(f"ffprobe failed (exit {result.returncode}): {result.stderr.strip()[:300]}")

    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise ToolError(f"ffprobe returned invalid JSON: {exc}") from exc

    try:
        duration = float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError) as exc:
        raise ToolError("ffprobe did not report a container duration") from exc

    audio: list[AudioStream] = []
    has_video = False
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            has_video = True
        elif codec_type == "audio":
            language = (stream.get("tags") or {}).get("language")
            audio.append(
                AudioStream(
                    codec=stream.get("codec_name"),
                    channels=_to_int(stream.get("channels")),
                    language=language.strip().lower() if language else None,
                )
            )
    return ProbeResult(duration_seconds=duration, audio_streams=audio, has_video=has_video)


# ---------------------------------------------------------------------------------------
# Tier 2 analysis
# ---------------------------------------------------------------------------------------

# Servarr reports languages by English name; containers tag them with ISO 639-2 codes
# (both the bibliographic "B" and terminologic "T" variants appear in the wild).
LANGUAGE_CODES: dict[str, frozenset[str]] = {
    "english": frozenset({"eng", "en"}),
    "french": frozenset({"fre", "fra", "fr"}),
    "german": frozenset({"ger", "deu", "de"}),
    "spanish": frozenset({"spa", "es"}),
    "italian": frozenset({"ita", "it"}),
    "portuguese": frozenset({"por", "pt"}),
    "japanese": frozenset({"jpn", "ja"}),
    "korean": frozenset({"kor", "ko"}),
    "chinese": frozenset({"chi", "zho", "zh", "cmn", "yue"}),
    "mandarin": frozenset({"chi", "zho", "zh", "cmn"}),
    "cantonese": frozenset({"chi", "zho", "yue"}),
    "russian": frozenset({"rus", "ru"}),
    "hindi": frozenset({"hin", "hi"}),
    "arabic": frozenset({"ara", "ar"}),
    "dutch": frozenset({"dut", "nld", "nl"}),
    "swedish": frozenset({"swe", "sv"}),
    "norwegian": frozenset({"nor", "nob", "nno", "no"}),
    "danish": frozenset({"dan", "da"}),
    "finnish": frozenset({"fin", "fi"}),
    "polish": frozenset({"pol", "pl"}),
    "czech": frozenset({"cze", "ces", "cs"}),
    "hungarian": frozenset({"hun", "hu"}),
    "turkish": frozenset({"tur", "tr"}),
    "greek": frozenset({"gre", "ell", "el"}),
    "hebrew": frozenset({"heb", "he"}),
    "thai": frozenset({"tha", "th"}),
    "vietnamese": frozenset({"vie", "vi"}),
    "indonesian": frozenset({"ind", "id"}),
    "ukrainian": frozenset({"ukr", "uk"}),
    "romanian": frozenset({"rum", "ron", "ro"}),
    "persian": frozenset({"per", "fas", "fa"}),
    "tamil": frozenset({"tam", "ta"}),
    "telugu": frozenset({"tel", "te"}),
}

_UNDETERMINED = {"und", "unk", "zxx", "mis", "mul", ""}


def check_audio_language(
    probe: ProbeResult, original_language: str | None
) -> tuple[bool | None, str]:
    """Verify at least one audio stream matches the original language.

    Returns ``(True/False, detail)`` or ``(None, detail)`` when the check is inconclusive
    (unknown language, no audio-language tags) so callers never reject on missing data.
    """
    if not original_language:
        return None, "no original language provided"
    wanted = LANGUAGE_CODES.get(original_language.strip().lower())
    if wanted is None:
        return None, f"original language {original_language!r} is not in the language table"
    if not probe.audio_streams:
        return False, "file contains no audio streams"

    tagged = [
        s.language for s in probe.audio_streams if s.language and s.language not in _UNDETERMINED
    ]
    if not tagged:
        return None, "audio streams carry no language tags"
    if any(code in wanted for code in tagged):
        return True, f"original language {original_language} present ({', '.join(tagged)})"
    return False, f"no {original_language} audio track (found: {', '.join(tagged)})"


_ADVERTISED_SURROUND = re.compile(r"atmos|true[\s._-]?hd|(?<!\d)7\.1(?!\d)", re.IGNORECASE)


def check_channel_sanity(probe: ProbeResult, advertised_text: str) -> tuple[bool | None, str]:
    """Flag releases advertising Atmos / TrueHD / 7.1 whose best audio track is only stereo."""
    match = _ADVERTISED_SURROUND.search(advertised_text)
    if not match:
        return None, "release does not advertise Atmos/TrueHD/7.1"
    channels = probe.audio_channels
    if not channels:
        return None, "no channel information reported by ffprobe"
    best = max(channels)
    if best <= 2:
        return False, f"advertised {match.group(0)!r} but best audio track has {best} channel(s)"
    return True, f"advertised {match.group(0)!r}; best audio track has {best} channels"
