"""Unit tests for Tier 0 pre-download inspection and qBittorrent API integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.config import Settings
from src.main import create_app
from src.models.history import HistoryStore, VerificationStatus
from src.services.notifier import Notifier
from src.services.pre_download_checker import PreDownloadInspector
from src.services.qbittorrent import QBittorrentClient
from src.services.remediator import Remediator


@pytest.fixture
def test_settings(tmp_path: Any) -> Settings:
    s = Settings()
    s.storage.database_path = str(tmp_path / "test.db")
    s.verification.dry_run = False
    s.sonarr.enabled = True
    s.sonarr.url = "http://sonarr.local"
    s.sonarr.api_key = SecretStr("sonarr-key")
    s.qbittorrent.enabled = True
    s.qbittorrent.url = "http://qbit.local:8080"
    s.qbittorrent.username = "admin"
    s.qbittorrent.password = SecretStr("adminpass")
    s.qbittorrent.inspect_on_grab = True
    s.qbittorrent.metadata_timeout_seconds = 5
    return s


@pytest.mark.anyio
async def test_qbittorrent_client_login_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/auth/login"
        assert "username=admin" in request.content.decode("utf-8")
        return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=test_cookie_123"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = QBittorrentClient(
            url="http://localhost:8080",
            username="admin",
            password="secretpassword",
            client=http_client,
        )
        ok = await client.login()
        assert ok is True
        assert client._authenticated is True


@pytest.mark.anyio
async def test_qbittorrent_client_login_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Fails.")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = QBittorrentClient(
            url="http://localhost:8080",
            username="admin",
            password="wrongpassword",
            client=http_client,
        )
        ok = await client.login()
        assert ok is False
        assert client._authenticated is False


@pytest.mark.anyio
async def test_qbittorrent_client_get_files_and_delete() -> None:
    deleted_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/files":
            assert request.url.params.get("hash") == "abc123hash"
            return httpx.Response(
                200,
                json=[
                    {"name": "Movie.2024.1080p.mkv", "size": 1048576},
                    {"name": "sample.nfo", "size": 1024},
                ],
            )
        if request.url.path == "/api/v2/torrents/delete":
            deleted_calls.append(request.content.decode("utf-8"))
            return httpx.Response(200, text="Ok.")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = QBittorrentClient(
            url="http://localhost:8080",
            username="admin",
            password="pass",
            client=http_client,
        )
        files = await client.get_torrent_files("abc123hash")
        assert len(files) == 2
        assert files[0]["name"] == "Movie.2024.1080p.mkv"

        del_ok = await client.delete_torrent("abc123hash", delete_files=True)
        assert del_ok is True
        assert len(deleted_calls) == 1
        assert "hashes=abc123hash" in deleted_calls[0]
        assert "deleteFiles=true" in deleted_calls[0]


@pytest.mark.anyio
async def test_pre_download_inspector_purges_malicious_archive(
    test_settings: Settings, tmp_path: Any
) -> None:
    torrent_hash = "4a76a8d6778f691d1e4e7195c64ef51381283626"
    release_title = "Show.Title.S01E01.720p.HDTV.x264-FLUX.7z"

    history = HistoryStore(str(tmp_path / "history.db"))
    await history.open()

    bad_dir = "Show.Title.S01E01.720p.HDTV.x264-FLUX"
    bad_name = f"{bad_dir}/{bad_dir}.7z"
    qbit_client = MagicMock(spec=QBittorrentClient)
    qbit_client.get_torrent_files = AsyncMock(
        return_value=[
            {"name": bad_name, "size": 524288000},
            {"name": f"{bad_dir}/instructions.txt", "size": 1024},
        ]
    )
    qbit_client.delete_torrent = AsyncMock(return_value=True)

    sonarr_failed_invoked = []
    sonarr_search_invoked = []

    def sonarr_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v3/history/failed/"):
            sonarr_failed_invoked.append(str(request.url))
            return httpx.Response(200, json={})
        if path in ("/api/v3/history/series", "/api/v3/history"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": 9942,
                            "eventType": "grabbed",
                            "sourceTitle": release_title,
                            "downloadId": torrent_hash,
                        }
                    ]
                },
            )
        if path == "/api/v3/command":
            sonarr_search_invoked.append(str(request.url))
            return httpx.Response(200, json={"status": "queued"})
        return httpx.Response(404)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(sonarr_handler))
    remediator = Remediator(test_settings, client=mock_http)
    notifier = MagicMock(spec=Notifier)
    notifier.send_discord = AsyncMock(return_value=True)

    inspector = PreDownloadInspector(
        settings=test_settings,
        history=history,
        remediator=remediator,
        notifier=notifier,
        qbit_client=qbit_client,
    )

    grab_event = {
        "eventType": "Grab",
        "downloadClient": "qBittorrent",
        "downloadId": torrent_hash,
        "series": {"id": 10, "title": "Show Title"},
        "episodes": [{"id": 101, "episodeNumber": 1, "seasonNumber": 1, "title": "Pilot"}],
        "release": {
            "releaseTitle": release_title,
            "indexer": "TestIndexer",
            "size": 524288000,
        },
    }

    result = await inspector.inspect_grab_event(grab_event)
    assert result is True

    # 1. Assert delete_torrent is called with deleteFiles=true
    qbit_client.delete_torrent.assert_awaited_once_with(torrent_hash, delete_files=True)

    # 2. Assert Sonarr failed history endpoint is invoked
    assert len(sonarr_failed_invoked) == 1
    assert "9942" in sonarr_failed_invoked[0]

    # 3. Assert replacement search triggered
    assert len(sonarr_search_invoked) == 1

    # 4. Assert history database logged REJECTED_PRE_DOWNLOAD
    entries = await history.recent(limit=10)
    assert len(entries) == 1
    assert entries[0].status == VerificationStatus.REJECTED_PRE_DOWNLOAD
    assert entries[0].release_name == release_title
    assert ".7z" in entries[0].failure_reasons[0]

    # 5. Assert Discord alert was dispatched
    notifier.send_discord.assert_awaited_once()
    call_args = notifier.send_discord.await_args
    assert "🚨 Fake Release Blocked" in call_args.kwargs.get("title", "")

    await history.close()
    await mock_http.aclose()


@pytest.mark.anyio
async def test_pre_download_inspector_passes_legitimate_video(
    test_settings: Settings, tmp_path: Any
) -> None:
    history = HistoryStore(str(tmp_path / "history.db"))
    await history.open()

    qbit_client = MagicMock(spec=QBittorrentClient)
    qbit_client.get_torrent_files = AsyncMock(
        return_value=[
            {"name": "Legit.Movie.2024.1080p.mkv", "size": 4200000000},
            {"name": "sample.nfo", "size": 1024},
        ]
    )
    qbit_client.delete_torrent = AsyncMock()

    remediator = MagicMock(spec=Remediator)
    remediator.remove_from_queue_and_blocklist = AsyncMock()
    remediator.blocklist_release = AsyncMock()
    notifier = MagicMock(spec=Notifier)

    inspector = PreDownloadInspector(
        settings=test_settings,
        history=history,
        remediator=remediator,
        notifier=notifier,
        qbit_client=qbit_client,
    )

    grab_event = {
        "eventType": "Grab",
        "downloadClient": "qBittorrent",
        "downloadId": "clean_hash_456",
        "movie": {"id": 20, "title": "Legit Movie"},
        "release": {
            "releaseTitle": "Legit.Movie.2024.1080p.mkv",
            "indexer": "TestIndexer",
            "size": 4200000000,
        },
    }

    result = await inspector.inspect_grab_event(grab_event)
    assert result is True

    # Confirm legitimate file is NOT deleted and NOT blocklisted
    qbit_client.delete_torrent.assert_not_awaited()
    remediator.remove_from_queue_and_blocklist.assert_not_awaited()
    remediator.blocklist_release.assert_not_awaited()

    entries = await history.recent(limit=10)
    assert len(entries) == 0

    await history.close()


@pytest.mark.anyio
async def test_pre_download_inspector_purges_zero_video_container_torrent(
    test_settings: Settings, tmp_path: Any
) -> None:
    history = HistoryStore(str(tmp_path / "history.db"))
    await history.open()

    qbit_client = MagicMock(spec=QBittorrentClient)
    qbit_client.get_torrent_files = AsyncMock(
        return_value=[
            {"name": "instructions.txt", "size": 500},
            {"name": "sample.nfo", "size": 200},
        ]
    )
    qbit_client.delete_torrent = AsyncMock(return_value=True)

    remediator = MagicMock(spec=Remediator)
    remediator.remove_from_queue_and_blocklist = AsyncMock(return_value="Blocklisted")
    remediator.blocklist_release = AsyncMock(return_value="Blocklisted")
    remediator.trigger_search = AsyncMock(return_value="Triggered")
    notifier = MagicMock(spec=Notifier)
    notifier.send_discord = AsyncMock()

    inspector = PreDownloadInspector(
        settings=test_settings,
        history=history,
        remediator=remediator,
        notifier=notifier,
        qbit_client=qbit_client,
    )

    grab_event = {
        "eventType": "Grab",
        "downloadClient": "qBittorrent",
        "downloadId": "hash_no_video",
        "movie": {"id": 30, "title": "Fake Movie"},
        "release": {"releaseTitle": "Fake.Movie.NoVideo", "size": 700},
    }

    result = await inspector.inspect_grab_event(grab_event)
    assert result is True

    qbit_client.delete_torrent.assert_awaited_once_with("hash_no_video", delete_files=True)
    remediator.remove_from_queue_and_blocklist.assert_awaited_once()

    entries = await history.recent(limit=10)
    assert len(entries) == 1
    assert entries[0].status == VerificationStatus.REJECTED_PRE_DOWNLOAD
    assert "Zero valid video containers" in entries[0].failure_reasons[0]

    await history.close()


@pytest.mark.anyio
async def test_pre_download_inspector_skips_non_qbittorrent(test_settings: Settings) -> None:
    qbit_client = MagicMock(spec=QBittorrentClient)
    inspector = PreDownloadInspector(
        settings=test_settings,
        history=MagicMock(),
        remediator=MagicMock(),
        notifier=MagicMock(),
        qbit_client=qbit_client,
    )

    grab_event = {
        "eventType": "Grab",
        "downloadClient": "Transmission",
        "downloadId": "hash_trans",
    }
    result = await inspector.inspect_grab_event(grab_event)
    assert result is False
    qbit_client.get_torrent_files.assert_not_called()


def test_webhook_endpoint_grab_routing(test_settings: Settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        # When qbittorrent is enabled and inspect_on_grab is True
        with patch.object(
            app.state.pre_download_checker, "inspect_grab_event", new_callable=AsyncMock
        ) as mock_inspect:
            mock_inspect.return_value = True
            resp = client.post(
                "/api/v1/webhook",
                json={
                    "eventType": "Grab",
                    "downloadClient": "qBittorrent",
                    "downloadId": "test_hash_123",
                    "series": {"id": 1, "title": "Test Series"},
                },
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert resp.json()["message"] == "Pre-download inspection initiated"

        # When inspect_on_grab is disabled
        test_settings.qbittorrent.inspect_on_grab = False
        resp = client.post(
            "/api/v1/webhook",
            json={
                "eventType": "Grab",
                "downloadClient": "qBittorrent",
                "downloadId": "test_hash_123",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


def test_settings_test_qbittorrent_endpoint(test_settings: Settings) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        with patch(
            "src.services.qbittorrent.QBittorrentClient.test_connection",
            new_callable=AsyncMock,
        ) as mock_test:
            mock_test.return_value = (True, "Connected to qBittorrent (WebAPI v2.9.3)")
            resp = client.post(
                "/api/v1/settings/test-qbittorrent",
                json={
                    "url": "http://localhost:8080",
                    "username": "admin",
                    "password": "pass",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            assert "Connected to qBittorrent" in resp.json()["message"]

            mock_test.return_value = (False, "Invalid credentials")
            resp = client.post(
                "/api/v1/settings/test-qbittorrent",
                json={
                    "url": "http://localhost:8080",
                    "username": "admin",
                    "password": "wrong",
                },
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "error"
            assert "Invalid credentials" in resp.json()["message"]


def test_simulate_event_grab_bad_file_payload() -> None:
    from scripts.simulate_event import build_payload

    payload = build_payload(
        file_path="",
        title="Simulated Series",
        expected_runtime=60,
        event_type="grab",
        bad_file=True,
    )
    assert payload["eventType"] == "Grab"
    assert payload["downloadClient"] == "qBittorrent"
    assert payload["release"]["releaseTitle"].endswith(".7z")
    assert payload["downloadId"] == "4a76a8d6778f691d1e4e7195c64ef51381283626"


@pytest.mark.anyio
async def test_pre_download_inspector_queue_blocklist_workflow(
    test_settings: Settings, tmp_path: Any
) -> None:
    torrent_hash = "5b87b9e7889f702e2f5f8206d75ff62492394737"
    release_title = "Show.Title.S02E05.720p.HDTV.x264.zip"

    history = HistoryStore(str(tmp_path / "history.db"))
    await history.open()

    qbit_client = MagicMock(spec=QBittorrentClient)
    qbit_client.get_torrent_files = AsyncMock(
        return_value=[
            {"name": "malicious.zip", "size": 100000000},
        ]
    )
    qbit_client.delete_torrent = AsyncMock(return_value=True)

    queue_delete_invoked = []
    blocklist_post_invoked = []
    search_invoked = []

    def sonarr_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/queue" and request.method == "GET":
            assert request.url.params.get("includeUnknownSeriesItems") == "true"
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": 77,
                            "downloadId": torrent_hash.upper(),
                            "title": release_title,
                        }
                    ]
                },
            )
        if path == "/api/v3/queue/77" and request.method == "DELETE":
            queue_delete_invoked.append((str(request.url), dict(request.url.params)))
            return httpx.Response(200, json={})
        if path == "/api/v3/blocklist" and request.method == "POST":
            blocklist_post_invoked.append(str(request.url))
            return httpx.Response(405)
        if path == "/api/v3/command":
            search_invoked.append(str(request.url))
            return httpx.Response(200, json={"status": "queued"})
        return httpx.Response(404)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(sonarr_handler))
    remediator = Remediator(test_settings, client=mock_http)
    notifier = MagicMock(spec=Notifier)
    notifier.send_discord = AsyncMock(return_value=True)

    inspector = PreDownloadInspector(
        settings=test_settings,
        history=history,
        remediator=remediator,
        notifier=notifier,
        qbit_client=qbit_client,
    )

    grab_event = {
        "eventType": "Grab",
        "downloadClient": "qBittorrent",
        "downloadId": torrent_hash,
        "series": {"id": 15, "title": "Show Title"},
        "episodes": [{"id": 205}],
        "release": {
            "releaseTitle": release_title,
            "indexer": "TestIndexer",
            "size": 100000000,
        },
    }

    result = await inspector.inspect_grab_event(grab_event)
    assert result is True

    # Assert qBittorrent deletion
    qbit_client.delete_torrent.assert_awaited_once_with(torrent_hash, delete_files=True)

    # Assert Servarr queue deletion was called with exact params
    assert len(queue_delete_invoked) == 1
    url_str, params = queue_delete_invoked[0]
    assert "/api/v3/queue/77" in url_str
    assert params.get("blocklist") == "true"
    assert params.get("skipRedownload") == "false"
    assert params.get("removeFromClient") == "false"

    # Assert Sonarr POST /api/v3/blocklist was NEVER called
    assert len(blocklist_post_invoked) == 0

    # Assert replacement search triggered
    assert len(search_invoked) == 1

    # Assert history recorded
    entries = await history.recent(limit=10)
    assert len(entries) == 1
    assert entries[0].status == VerificationStatus.REJECTED_PRE_DOWNLOAD
    assert "Removed queue item 77 and blocklisted release" in entries[0].remediation

    await history.close()
    await mock_http.aclose()


@pytest.mark.anyio
async def test_remediator_queue_blocklist_fallback_to_history(test_settings: Settings) -> None:
    download_id = "test_download_hash_fallback"
    queue_called = []
    history_failed_called = []

    def sonarr_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/queue":
            queue_called.append(path)
            return httpx.Response(200, json={"records": []})
        if path.startswith("/api/v3/history/failed/"):
            history_failed_called.append(path)
            return httpx.Response(200, json={})
        if path in ("/api/v3/history/series", "/api/v3/history"):
            return httpx.Response(
                200,
                json={
                    "records": [
                        {
                            "id": 1234,
                            "eventType": "grabbed",
                            "sourceTitle": "Test.Release.Title",
                            "downloadId": download_id,
                        }
                    ]
                },
            )
        return httpx.Response(404)

    mock_http = httpx.AsyncClient(transport=httpx.MockTransport(sonarr_handler))
    remediator = Remediator(test_settings, client=mock_http)

    res = await remediator.remove_from_queue_and_blocklist(
        "sonarr",
        download_id,
        release_title="Test.Release.Title",
        entity_id=1,
    )
    assert len(queue_called) == 1
    assert len(history_failed_called) == 1
    assert "/api/v3/history/failed/1234" in history_failed_called[0]
    assert "Blocklisted release via history record 1234" in res

    await mock_http.aclose()
