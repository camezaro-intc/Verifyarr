"""Tests for update manager service and system update endpoints."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import create_app
from src.services.updater import UpdateManager, parse_semver


def _make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        verification={
            "dry_run": True,
            "runtime_delta_threshold_minutes": 15,
            "max_concurrent_probes": 2,
        },
        storage={"database_path": str(tmp_path / "test.db")},
    )


def test_parse_semver() -> None:
    assert parse_semver("v1.6.0") == (1, 6, 0)
    assert parse_semver("1.5.6") == (1, 5, 6)
    assert parse_semver("v2.0") == (2, 0)
    assert parse_semver("invalid") == (0,)
    assert parse_semver("v1.6.0") > parse_semver("1.5.6")
    assert parse_semver("1.5.6") == parse_semver("v1.5.6")


@pytest.mark.anyio
async def test_check_for_updates_available() -> None:
    mock_payload = {
        "tag_name": "v1.6.0",
        "html_url": "https://github.com/camezaro-intc/Verifyarr/releases/tag/v1.6.0",
        "body": "### New Features\n- Live update manager",
        "assets": [
            {
                "name": "verifyarr-v1.6.0.zip",
                "browser_download_url": "https://github.com/camezaro-intc/Verifyarr/releases/download/v1.6.0/verifyarr-v1.6.0.zip",
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Verifyarr/" in request.headers.get("User-Agent", "")
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        manager = UpdateManager(
            current_version="1.5.6",
            repo_api_url="https://api.github.com/mock/releases/latest",
            http_client=client,
        )
        result = await manager.check_for_updates()

    assert result["update_available"] is True
    assert result["current_version"] == "1.5.6"
    assert result["latest_version"] == "1.6.0"
    assert (
        result["download_url"]
        == "https://github.com/camezaro-intc/Verifyarr/releases/download/v1.6.0/verifyarr-v1.6.0.zip"
    )
    assert "Live update manager" in result["changelog"]


@pytest.mark.anyio
async def test_check_for_updates_not_available() -> None:
    mock_payload = {
        "tag_name": "v1.5.6",
        "html_url": "https://github.com/camezaro-intc/Verifyarr/releases/tag/v1.5.6",
        "body": "Up to date",
        "assets": [],
        "zipball_url": "https://github.com/camezaro-intc/Verifyarr/legacy.zip",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        manager = UpdateManager(
            current_version="1.5.6",
            repo_api_url="https://api.github.com/mock/releases/latest",
            http_client=client,
        )
        result = await manager.check_for_updates()

    assert result["update_available"] is False
    assert result["download_url"] == "https://github.com/camezaro-intc/Verifyarr/legacy.zip"


@pytest.mark.anyio
async def test_check_for_updates_network_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        manager = UpdateManager(
            current_version="1.5.6",
            repo_api_url="https://api.github.com/mock/releases/latest",
            http_client=client,
        )
        result = await manager.check_for_updates(force=True)

    assert result["update_available"] is False
    assert result["latest_version"] == "1.5.6"


@pytest.mark.anyio
async def test_check_for_updates_rate_limit_fallback() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(403, json={"message": "API rate limit exceeded"})
        if "github.com/camezaro-intc/Verifyarr/releases/latest" in url:
            return httpx.Response(
                302,
                headers={"Location": "https://github.com/camezaro-intc/Verifyarr/releases/tag/v1.6.11"},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        manager = UpdateManager(
            current_version="1.5.6",
            repo_api_url="https://api.github.com/mock/releases/latest",
            http_client=client,
        )
        result = await manager.check_for_updates(force=True)

    assert result["update_available"] is True
    assert result["latest_version"] == "1.6.11"
    assert "v1.6.11/Verifyarr-windows-x64.zip" in result["download_url"]


def test_apply_update_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_dir = tmp_path / "Verifyarr"
    temp_dir = install_dir / "temp"
    install_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VERIFYARR_INSTALL_DIR", str(install_dir))
    monkeypatch.setenv("VERIFYARR_TEMP_DIR", str(temp_dir))
    monkeypatch.setenv("VERIFYARR_TEST_NO_EXIT", "1")

    # Create dummy in-memory zip content
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("test_file.txt", "updated contents")
        zf.writestr("src/test_code.py", "print('hello')")
    zip_bytes = zip_buffer.getvalue()

    def mock_get(url: str, *args, **kwargs) -> httpx.Response:
        return httpx.Response(200, content=zip_bytes, request=httpx.Request("GET", url))

    with (
        patch("httpx.Client.get", side_effect=mock_get),
        patch("subprocess.Popen") as mock_popen,
    ):
        manager = UpdateManager(current_version="1.5.6")
        manager.apply_update("https://example.com/update.zip", exit_process=False)

        # Check update.zip exists
        assert (temp_dir / "update.zip").exists()

        # Check extracted staging file
        staging_file = temp_dir / "staging" / "test_file.txt"
        assert staging_file.exists()
        assert staging_file.read_text(encoding="utf-8") == "updated contents"

        # Check updater.ps1 was written
        updater_ps1 = temp_dir / "updater.ps1"
        assert updater_ps1.exists()
        script_text = updater_ps1.read_text(encoding="utf-8")
        assert "nssm stop Verifyarr" in script_text
        assert "nssm start Verifyarr" in script_text
        assert "Copy-Item" in script_text

        # Verify subprocess.Popen was called with new process group flag
        assert mock_popen.called
        cmd_args, kwargs = mock_popen.call_args
        if os.name == "nt":
            flags = kwargs.get("creationflags", 0)
            assert bool(flags & 0x00000200)


def test_system_update_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(_make_test_settings(tmp_path))

    with TestClient(app) as client:
        # Mock check_for_updates
        with patch.object(
            UpdateManager,
            "check_for_updates",
            return_value={
                "update_available": True,
                "current_version": "1.5.6",
                "latest_version": "1.6.0",
                "release_url": "https://github.com/camezaro-intc/Verifyarr/releases/tag/v1.6.0",
                "changelog": "Changelog details",
                "download_url": "https://example.com/asset.zip",
            },
        ):
            resp = client.get("/api/v1/system/update/check")
            assert resp.status_code == 200
            data = resp.json()
            assert data["update_available"] is True
            assert data["latest_version"] == "1.6.0"

        # Test apply update endpoint with invalid payload
        invalid_resp = client.post("/api/v1/system/update/apply", json={"download_url": ""})
        assert invalid_resp.status_code == 400

        # Test apply update endpoint with valid payload
        with patch.object(UpdateManager, "apply_update", return_value=None) as mock_apply:
            valid_resp = client.post(
                "/api/v1/system/update/apply",
                json={"download_url": "https://example.com/asset.zip"},
            )
            assert valid_resp.status_code == 200
            assert valid_resp.json() == {"status": "restarting"}
            assert mock_apply.called
            mock_apply.assert_called_once_with("https://example.com/asset.zip")


def test_apply_update_delegates_service_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_dir = tmp_path / "Verifyarr"
    temp_dir = install_dir / "temp"
    install_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("VERIFYARR_INSTALL_DIR", str(install_dir))
    monkeypatch.setenv("VERIFYARR_TEMP_DIR", str(temp_dir))
    monkeypatch.delenv("VERIFYARR_TEST_NO_EXIT", raising=False)

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        zf.writestr("test_file.txt", "updated contents")
    zip_bytes = zip_buffer.getvalue()

    def mock_get(url: str, *args, **kwargs) -> httpx.Response:
        return httpx.Response(200, content=zip_bytes, request=httpx.Request("GET", url))

    with (
        patch("httpx.Client.get", side_effect=mock_get),
        patch("subprocess.Popen") as mock_popen,
        patch("os._exit") as mock_exit,
    ):
        manager = UpdateManager(current_version="1.5.6")
        manager.apply_update("https://example.com/update.zip", exit_process=True)

        assert mock_popen.called
        # Verify os._exit is NOT called prematurely (preventing NSSM crash-restart loops)
        assert not mock_exit.called

