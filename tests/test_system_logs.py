"""Tests for system endpoints: live log viewer, filtering, and log download."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import create_app


def _make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        verification={
            "dry_run": True,
            "runtime_delta_threshold_minutes": 15,
            "max_concurrent_probes": 2,
        },
        storage={"database_path": str(tmp_path / "test.db")},
    )


SAMPLE_LOG_CONTENT = """2026-10-23T14:00:00-06:00 INFO     src.main: Starting Verifyarr 1.5.4
2026-10-23T14:00:01-06:00 DEBUG    src.services.prober: Running: ffprobe -version
2026-10-23T14:00:02-06:00 WARNING  src.services.pre_download_checker: Suspicious release
2026-10-23T14:00:03-06:00 ERROR    src.services.remediator: Failed to connect to Sonarr
Non-standard unstructured stack trace line
2026-10-23T14:00:05-06:00 INFO     uvicorn.access: 127.0.0.1:50000 - "GET / HTTP/1.1" 200
"""


def test_system_logs_endpoint_when_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    non_existent = tmp_path / "does_not_exist.log"
    monkeypatch.setenv("VERIFYARR_LOG_PATH", str(non_existent))

    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        resp = client.get("/api/v1/system/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["lines"] == []
        assert data["total"] == 0
        assert data["file_exists"] is False

        download_resp = client.get("/api/v1/system/logs/download")
        assert download_resp.status_code == 404
        assert "not found" in download_resp.json()["detail"].lower()


def test_system_logs_endpoint_parsing_and_filtering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "verifyarr.log"
    log_file.write_text(SAMPLE_LOG_CONTENT, encoding="utf-8")
    monkeypatch.setenv("VERIFYARR_LOG_PATH", str(log_file))

    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        # 1. Fetch all logs
        resp = client.get("/api/v1/system/logs?lines=100")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 6
        assert data["file_exists"] is True

        first = data["lines"][0]
        assert first["timestamp"] == "2026-10-23T14:00:00-06:00"
        assert first["level"] == "INFO"
        assert first["logger"] == "src.main"
        assert "Starting Verifyarr" in first["message"]

        # 2. Filter by level WARNING
        warn_resp = client.get("/api/v1/system/logs?level=WARNING")
        assert warn_resp.status_code == 200
        warn_data = warn_resp.json()
        assert warn_data["total"] == 1
        assert warn_data["lines"][0]["level"] == "WARNING"
        assert "Suspicious release" in warn_data["lines"][0]["message"]

        # 3. Filter by level ERROR
        err_resp = client.get("/api/v1/system/logs?level=ERROR")
        assert err_resp.status_code == 200
        err_data = err_resp.json()
        assert err_data["total"] == 1
        assert err_data["lines"][0]["level"] == "ERROR"
        assert "Failed to connect" in err_data["lines"][0]["message"]

        # 4. Filter by text search
        search_resp = client.get("/api/v1/system/logs?search=ffprobe")
        assert search_resp.status_code == 200
        search_data = search_resp.json()
        assert search_data["total"] == 1
        assert "Running: ffprobe" in search_data["lines"][0]["message"]

        # 5. Download log file
        download_resp = client.get("/api/v1/system/logs/download")
        assert download_resp.status_code == 200
        assert download_resp.headers["content-type"].startswith("text/plain")
        assert "Starting Verifyarr" in download_resp.text
