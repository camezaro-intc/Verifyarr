"""Tests for Correlated Log Aggregator Service and API Endpoint."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import create_app
from src.models.history import HistoryEntry, HistoryStore, VerificationStatus
from src.services.log_aggregator import (
    LogAggregator,
    extract_keywords,
    is_in_window,
    normalize_level,
)


def test_helper_functions() -> None:
    # Level normalization
    assert normalize_level("WARNING") == "WARN"
    assert normalize_level("WARN") == "WARN"
    assert normalize_level("WRN") == "WARN"
    assert normalize_level("ERROR") == "ERROR"
    assert normalize_level("ERR") == "ERROR"
    assert normalize_level("CRITICAL") == "ERROR"
    assert normalize_level("FATAL") == "ERROR"
    assert normalize_level("FTL") == "ERROR"
    assert normalize_level("INFO") == "INFO"
    assert normalize_level("INF") == "INFO"
    assert normalize_level("DEBUG") == "INFO"

    # Keywords extraction
    kws = extract_keywords("South.Park.S29E01.1080p.WEB-DL.DDP5.1.H.264-FLUX", "South Park")
    assert "south" in kws
    assert "park" in kws
    assert "s29e01" in kws
    assert "1080p" not in kws
    assert "web-dl" not in kws

    # Window check
    base = datetime(2026, 10, 23, 14, 0, 0, tzinfo=UTC)
    w_start = base - timedelta(minutes=45)
    w_end = base + timedelta(minutes=45)

    assert is_in_window(base, w_start, w_end)
    assert is_in_window(base - timedelta(minutes=40), w_start, w_end)
    assert is_in_window(base + timedelta(minutes=40), w_start, w_end)
    assert not is_in_window(base - timedelta(minutes=50), w_start, w_end)
    assert not is_in_window(base + timedelta(minutes=50), w_start, w_end)


@pytest.mark.anyio
async def test_aggregator_missing_files_graceful(tmp_path: Path) -> None:
    # Ensure missing paths do not cause errors or exceptions
    history_db = str(tmp_path / "hist.db")
    store = HistoryStore(history_db)
    await store.open()

    entry = HistoryEntry(
        app="sonarr",
        title="Breaking Bad",
        release_name="Breaking.Bad.S01E01.720p",
        status=VerificationStatus.PASSED,
        download_hash="abcdef0123456789abcdef0123456789abcdef01",
    )
    saved = await store.add(entry)

    # Point env vars to non-existent locations
    os.environ["VERIFYARR_LOG_PATH"] = str(tmp_path / "no_verifyarr.log")
    os.environ["SONARR_LOG_PATH"] = str(tmp_path / "no_sonarr.txt")
    os.environ["JELLYFIN_LOG_DIR"] = str(tmp_path / "no_jellyfin_dir")

    # Mock qBittorrent client raising connection failure
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        aggregator = LogAggregator(
            settings=Settings(),
            history_store=store,
            http_client=http_client,
        )
        result = await aggregator.get_correlated_logs(saved.id)

    assert result is not None
    assert result["title"] == "Breaking Bad"
    assert result["release_name"] == "Breaking.Bad.S01E01.720p"
    assert result["events"] == []

    await store.close()


@pytest.mark.anyio
async def test_aggregator_collects_and_sorts_all_sources(tmp_path: Path) -> None:
    base_time = datetime(2026, 10, 23, 14, 30, 0, tzinfo=UTC)
    hash_str = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"

    history_db = str(tmp_path / "hist.db")
    store = HistoryStore(history_db)
    await store.open()

    entry = HistoryEntry(
        timestamp=base_time,
        app="sonarr",
        title="South Park",
        release_name="South.Park.S29E01.1080p.WEB-DL-FLUX",
        file_path=r"C:\Media\South Park\South.Park.S29E01.mkv",
        status=VerificationStatus.PASSED,
        download_hash=hash_str,
    )
    saved = await store.add(entry)

    # 1. Create Verifyarr log file
    t_v1 = (base_time + timedelta(minutes=2)).isoformat()
    t_v2 = (base_time + timedelta(minutes=3)).isoformat()
    verifyarr_log = tmp_path / "verifyarr.log"
    verifyarr_log.write_text(
        f"{t_v1} INFO     src.services.verifier: PASSED for South.Park.S29E01\n"
        f"{t_v2} WARNING  src.services.notifier: Discord warn South Park\n",
        encoding="utf-8",
    )
    os.environ["VERIFYARR_LOG_PATH"] = str(verifyarr_log)

    # 2. Create Sonarr log file (naive timestamps in local system time)
    local_base = base_time.astimezone()
    t_s1 = (local_base - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S.0")
    t_s2 = (local_base - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S.0")
    t_s3 = (local_base - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S.0")
    sonarr_log = tmp_path / "sonarr.txt"
    sonarr_log.write_text(
        f"{t_s1}|Info|DownloadDecisionMaker|Processing release for South Park\n"
        f"{t_s2}|Error|TrackedDownloadService|Failed import with hash {hash_str}\n"
        f"{t_s3}|Info|OtherSeries|Outside 45-min window\n",
        encoding="utf-8",
    )
    os.environ["SONARR_LOG_PATH"] = str(sonarr_log)

    # 3. Create Jellyfin log directory with latest log file
    t_j1 = (base_time + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.000 +00:00")
    t_j2 = (base_time + timedelta(minutes=6)).strftime("%Y-%m-%d %H:%M:%S.000 +00:00")
    jellyfin_dir = tmp_path / "jellyfin_logs"
    jellyfin_dir.mkdir()
    jellyfin_log = jellyfin_dir / "log_20261023.log"
    jellyfin_log.write_text(
        f"[{t_j1}] [INF] [1] Jellyfin: Found South.Park.S29E01.mkv\n"
        f"[{t_j2}] [WRN] [1] Jellyfin: Probe warn South.Park.S29E01.mkv\n",
        encoding="utf-8",
    )
    os.environ["JELLYFIN_LOG_DIR"] = str(jellyfin_dir)

    # 4. Mock qBittorrent API response
    qbit_time = int((base_time - timedelta(minutes=20)).timestamp() * 1000)
    qbit_logs = [
        {
            "id": 1,
            "message": f"Added torrent {hash_str} successfully",
            "timestamp": qbit_time,
            "type": 1,
        },
        {
            "id": 2,
            "message": "Torrent South.Park metadata resolved",
            "timestamp": qbit_time + 60000,
            "type": 4,
        },
        {
            "id": 3,
            "message": "Unrelated torrent added",
            "timestamp": qbit_time + 120000,
            "type": 1,
        },
    ]

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/api/v2/log/main" in str(request.url):
            return httpx.Response(200, json=qbit_logs)
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        aggregator = LogAggregator(
            settings=Settings(),
            history_store=store,
            http_client=http_client,
        )
        result = await aggregator.get_correlated_logs(saved.id)

    assert result is not None
    assert result["title"] == "South Park"
    assert result["release_name"] == "South.Park.S29E01.1080p.WEB-DL-FLUX"

    events = result["events"]
    assert len(events) >= 6

    # Verify sources exist
    apps = {e["app"] for e in events}
    assert "qBittorrent" in apps
    assert "Verifyarr" in apps
    assert "Sonarr" in apps
    assert "Jellyfin" in apps

    # Verify chronological ascending sort order
    timestamps = [datetime.fromisoformat(e["timestamp"]) for e in events]
    for i in range(len(timestamps) - 1):
        assert timestamps[i] <= timestamps[i + 1]

    # Verify levels normalization
    for e in events:
        assert e["level"] in ("INFO", "WARN", "ERROR")
        assert "message" in e

    await store.close()


def test_api_correlated_logs_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        storage={"database_path": str(tmp_path / "test.db")},
        verification={"dry_run": True},
    )

    # Set up mock verifyarr log file
    mock_log = tmp_path / "verifyarr.log"
    mock_log.write_text(
        "2026-10-23T14:30:00+00:00 INFO     src.services.verifier: PASSED Test.Release\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VERIFYARR_LOG_PATH", str(mock_log))
    monkeypatch.setenv("SONARR_LOG_PATH", str(tmp_path / "missing_sonarr.log"))
    monkeypatch.setenv("JELLYFIN_LOG_DIR", str(tmp_path / "missing_jellyfin"))

    app = create_app(settings)
    with TestClient(app) as client:
        # Non-existent ID returns 404
        resp_404 = client.get("/api/v1/history/99999/correlated-logs")
        assert resp_404.status_code == 404

        # Add history entry to SQLite
        store: HistoryStore = app.state.history
        import asyncio

        entry = HistoryEntry(
            timestamp=datetime(2026, 10, 23, 14, 30, 0, tzinfo=UTC),
            app="sonarr",
            title="Test Movie",
            release_name="Test.Release",
            status=VerificationStatus.PASSED,
        )
        saved = asyncio.run(store.add(entry))

        # Query endpoint
        resp = client.get(f"/api/v1/history/{saved.id}/correlated-logs")
        assert resp.status_code == 200
        data = resp.json()

        assert data["title"] == "Test Movie"
        assert data["release_name"] == "Test.Release"
        assert isinstance(data["events"], list)
        assert len(data["events"]) >= 1
        assert data["events"][0]["app"] == "Verifyarr"
        assert data["events"][0]["level"] == "INFO"

        # Test dashboard HTML renders LOGS ALL button and trace modal
        dash_resp = client.get("/")
        assert dash_resp.status_code == 200
        dash_html = dash_resp.text
        assert f"openCorrelatedLogs({saved.id})" in dash_html
        assert "LOGS ALL" in dash_html
        assert "btn-logs-all" in dash_html
        assert 'id="correlated-logs-modal"' in dash_html
        assert 'id="trace-terminal-output"' in dash_html


def test_token_extraction_and_matching() -> None:
    from src.services.log_aggregator import extract_search_tokens, matches_log_line

    tokens = extract_search_tokens(
        title="Avatar: Seven Havens - S01E08",
        release_name="Avatar Seven Havens S01E08 1080p WEB h264 GRACE",
        download_hash="11223344556677889900aabbccddeeff11223344",
    )

    assert "s01e08" in tokens["episode_codes"]
    assert "1x08" in tokens["episode_codes"]
    assert "avatar" in tokens["series_names"]
    assert tokens["download_hash"] == "11223344556677889900aabbccddeeff11223344"

    # Match by hash
    assert matches_log_line(
        "torrent with hash 11223344556677889900aabbccddeeff11223344 added", tokens
    )

    # Match by series name AND episode code
    assert matches_log_line("imported: avatar seven havens s01e08 successfully", tokens)
    assert matches_log_line("downloading avatar.seven.havens.s01e08.1080p...", tokens)
    assert matches_log_line("sonarr: avatar - 1x08 grabbed", tokens)

    # Reject if wrong episode
    assert not matches_log_line("imported: avatar seven havens s01e09 successfully", tokens)
    # Reject if wrong series
    assert not matches_log_line("imported: legend of korra s01e08 successfully", tokens)


@pytest.mark.anyio
async def test_qbittorrent_auth_retry_on_403() -> None:
    calls = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("Referer") == "http://localhost:8080/"

        if "/api/v2/log/main" in str(request.url) and len(calls) == 1:
            # First request forbidden
            return httpx.Response(403, text="Forbidden")
        if "/api/v2/auth/login" in str(request.url):
            return httpx.Response(200, text="Ok.")
        if "/api/v2/log/main" in str(request.url):
            return httpx.Response(200, json=[{
                "id": 1,
                "message": "Avatar S01E08 downloaded",
                "timestamp": int(datetime.now(UTC).timestamp() * 1000),
                "type": 1,
            }])
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        agg = LogAggregator(http_client=http_client)
        now = datetime.now(UTC)
        events = await agg.collect_qbittorrent(
            now - timedelta(minutes=45),
            now + timedelta(minutes=45),
            title="Avatar - S01E08",
            release_name="Avatar.S01E08",
        )

    assert len(events) == 1
    assert events[0]["app"] == "qBittorrent"
    assert "Avatar S01E08" in events[0]["message"]

