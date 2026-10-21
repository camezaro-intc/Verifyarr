"""Tier 1 (runtime delta) tests with ffprobe mocked, so no media files or binaries are needed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.services import verifier as verifier_module
from src.services.prober import ToolError, probe_file

from .conftest import ffprobe_json

FAKE_FILE = Path("movie.mkv")


def test_probe_file_parses_ffprobe_output(mock_ffprobe) -> None:
    calls = mock_ffprobe(ffprobe_json(10_380.0, audio_channels=8, language="ENG"))

    result = asyncio.run(probe_file(FAKE_FILE, ffprobe="ffprobe"))

    assert result.duration_seconds == pytest.approx(10_380.0)
    assert result.duration_minutes == pytest.approx(173.0)
    assert result.audio_codecs == ["eac3"]
    assert result.audio_channels == [8]
    assert result.languages == ["eng"]  # normalised to lower case
    argv = calls[0]
    assert argv[0] == "ffprobe" and argv[-1] == str(FAKE_FILE)
    assert "-show_entries" in argv and "json" in argv


def test_probe_file_raises_on_ffprobe_failure(mock_ffprobe) -> None:
    mock_ffprobe("", returncode=1, stderr="Invalid data found when processing input")

    with pytest.raises(ToolError, match="Invalid data"):
        asyncio.run(probe_file(FAKE_FILE, ffprobe="ffprobe"))


@pytest.mark.parametrize(
    ("probed_minutes", "expected_minutes"),
    [
        (173.0, 173),  # exact match
        (165.0, 173),  # shorter, inside tolerance
        (185.0, 173),  # longer, inside tolerance (extended cut)
        (158.0, 173),  # exactly at the 15 minute limit is still a pass
    ],
)
def test_runtime_delta_passes(
    mock_ffprobe, make_verifier, make_job, probed_minutes, expected_minutes
) -> None:
    mock_ffprobe(ffprobe_json(probed_minutes * 60))
    verifier = make_verifier(runtime_delta_threshold_minutes=15)

    checks, probe = asyncio.run(verifier._run_pipeline(make_job(expected_minutes), FAKE_FILE))

    assert probe.duration_minutes == pytest.approx(probed_minutes)
    assert len(checks) == 1
    assert checks[0].tier == 1
    assert checks[0].passed
    assert not checks[0].skipped


def test_runtime_delta_fails_for_mockbuster(mock_ffprobe, make_verifier, make_job) -> None:
    """An 86 minute knock-off carrying metadata for a 173 minute epic is rejected."""
    mock_ffprobe(ffprobe_json(86 * 60))
    verifier = make_verifier(runtime_delta_threshold_minutes=15)

    checks, probe = asyncio.run(verifier._run_pipeline(make_job(173), FAKE_FILE))

    assert probe.duration_minutes == pytest.approx(86.0)
    assert len(checks) == 1
    failed = checks[0]
    assert failed.tier == 1
    assert not failed.passed
    assert failed.name == "Runtime Mismatch"
    assert "86.0" in failed.detail and "173" in failed.detail


def test_runtime_failure_skips_later_tiers(
    mock_ffprobe, make_verifier, make_job, monkeypatch
) -> None:
    """Tier 1 is an immediate gate: Tier 2/3 must not run (or touch ffmpeg) after a mismatch."""
    mock_ffprobe(ffprobe_json(86 * 60))

    async def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("Tier 3 ran after a Tier 1 failure")

    monkeypatch.setattr(verifier_module, "detect_freeze_frames", must_not_run)
    verifier = make_verifier(enable_audio_sanity_check=True, enable_freeze_detection=True)

    checks, _ = asyncio.run(verifier._run_pipeline(make_job(173), FAKE_FILE))

    assert [c.tier for c in checks] == [1]
    assert not checks[0].passed


def test_runtime_threshold_is_configurable(mock_ffprobe, make_verifier, make_job) -> None:
    mock_ffprobe(ffprobe_json(150 * 60))  # 23 minutes short of 173

    strict = asyncio.run(
        make_verifier(runtime_delta_threshold_minutes=15)._run_pipeline(make_job(173), FAKE_FILE)
    )
    lenient = asyncio.run(
        make_verifier(runtime_delta_threshold_minutes=30)._run_pipeline(make_job(173), FAKE_FILE)
    )

    assert not strict[0][0].passed
    assert lenient[0][0].passed


def test_missing_expected_runtime_is_skipped_not_failed(
    mock_ffprobe, make_verifier, make_job
) -> None:
    mock_ffprobe(ffprobe_json(86 * 60))

    checks, _ = asyncio.run(
        make_verifier()._run_pipeline(make_job(expected_runtime_min=None), FAKE_FILE)
    )

    assert checks[0].passed and checks[0].skipped
