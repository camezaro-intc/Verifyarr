"""Shared fixtures: isolate settings from any local config.yaml / .env and fake ffprobe output."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.config import Settings
from src.models.servarr import VerificationJob
from src.services import prober
from src.services.prober import CommandResult
from src.services.verifier import Verifier


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read the developer's real config.yaml or .env during tests."""
    monkeypatch.setenv("VERIFYARR_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    monkeypatch.chdir(tmp_path)


def ffprobe_json(duration_seconds: float, audio_channels: int = 6, language: str = "eng") -> str:
    return json.dumps(
        {
            "streams": [
                {"codec_type": "video", "codec_name": "h264"},
                {
                    "codec_type": "audio",
                    "codec_name": "eac3",
                    "channels": audio_channels,
                    "tags": {"language": language},
                },
            ],
            "format": {"duration": f"{duration_seconds:.6f}"},
        }
    )


@pytest.fixture
def mock_ffprobe(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Replace ``prober.run_command`` with a fake ffprobe; returns the list of captured argv."""

    def install(stdout: str, returncode: int = 0, stderr: str = "") -> list[list[str]]:
        calls: list[list[str]] = []

        async def fake_run_command(args, timeout: float = 120) -> CommandResult:  # noqa: ANN001
            calls.append([str(a) for a in args])
            return CommandResult(returncode=returncode, stdout=stdout.encode(), stderr=stderr)

        monkeypatch.setattr(prober, "run_command", fake_run_command)
        return calls

    return install


@pytest.fixture
def make_verifier() -> Callable[..., Verifier]:
    """Build a Verifier wired for Tier 1 only (other tiers off unless requested)."""

    def build(**verification: object) -> Verifier:
        settings = Settings(
            verification={
                "enable_audio_sanity_check": False,
                "enable_freeze_detection": False,
                "enable_ocr_credit_check": False,
                **verification,
            }
        )
        verifier = Verifier(settings, MagicMock(), MagicMock(), MagicMock())
        verifier._ffprobe = Path("ffprobe")  # skip PATH discovery; run_command is mocked
        return verifier

    return build


@pytest.fixture
def make_job() -> Callable[..., VerificationJob]:
    def build(expected_runtime_min: float | None = 173, **overrides: object) -> VerificationJob:
        fields: dict[str, object] = {
            "app": "radarr",
            "entity_id": 1,
            "file_id": 10,
            "title": "Epic Movie (2020)",
            "release_title": "Epic.Movie.2020.1080p.BluRay.x264-GRP",
            "remote_path": "/media/Epic Movie (2020)/Epic.Movie.2020.mkv",
            "expected_runtime_min": expected_runtime_min,
        }
        fields.update(overrides)
        return VerificationJob(**fields)  # type: ignore[arg-type]

    return build
