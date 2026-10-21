"""Tests for web-based settings management, atomic YAML persistence, and in-memory hot reloading."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import AppConfig, Settings, save_config, save_config_atomic
from src.main import create_app


def _make_test_settings(tmp_path: Path) -> Settings:
    return Settings(
        verification={
            "dry_run": True,
            "runtime_delta_threshold_minutes": 15,
            "max_concurrent_probes": 2,
            "probe_timeout_seconds": 120,
        },
        radarr={
            "enabled": True,
            "url": "http://localhost:7878",
            "api_key": "test-radarr-secret-key",
        },
        sonarr={
            "enabled": False,
            "url": "http://localhost:8989",
            "api_key": "test-sonarr-secret-key",
        },
        jellyfin={
            "enabled": False,
            "url": "http://localhost:8096",
            "api_key": "test-jellyfin-secret-key",
        },
        storage={"database_path": str(tmp_path / "test_settings.db")},
    )


def test_atomic_yaml_writer(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.yaml"
    settings = _make_test_settings(tmp_path)

    save_config(settings, path=target)

    assert target.is_file()
    assert not target.with_suffix(target.suffix + ".tmp").exists()

    loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert loaded["verification"]["dry_run"] is True
    assert loaded["verification"]["runtime_delta_threshold_minutes"] == 15
    assert loaded["radarr"]["api_key"] == "test-radarr-secret-key"

    # Direct save_config_atomic call
    custom_target = tmp_path / "atomic.yaml"
    save_config_atomic({"verification": {"dry_run": False}}, custom_target)
    assert yaml.safe_load(custom_target.read_text(encoding="utf-8")) == {
        "verification": {"dry_run": False}
    }


def test_get_api_settings_returns_unveiled_keys_and_computed_fields(tmp_path: Path) -> None:
    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        res = client.get("/api/v1/settings")
        assert res.status_code == 200
        data = res.json()
        assert data["verification"]["dry_run"] is True
        assert data["verification"]["runtime_delta_threshold_minutes"] == 15
        assert data["verification"]["freeze_threshold_percent"] == 30.0
        assert data["radarr"]["api_key"] == "test-radarr-secret-key"
        assert data["sonarr"]["api_key"] == "test-sonarr-secret-key"
        assert data["jellyfin"]["api_key"] == "test-jellyfin-secret-key"


def test_post_api_settings_persists_to_disk_and_hot_reloads_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("VERIFYARR_CONFIG", str(cfg_file))

    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        # Initially in DRY-RUN mode
        page_before = client.get("/settings")
        assert page_before.status_code == 200
        assert "DRY-RUN" in page_before.text

        # Update settings: toggle dry_run -> False, adjust thresholds, add path mapping
        update_payload = {
            "verification": {
                "dry_run": False,
                "runtime_delta_threshold_minutes": 22,
                "max_concurrent_probes": 4,
                "probe_timeout_seconds": 90,
                "enable_audio_sanity_check": False,
                "enable_freeze_detection": True,
                "freeze_threshold_percent": 45,
                "enable_ocr_credit_check": True,
                "mockbuster_keywords": ["The Asylum", "Mock Studio"],
            },
            "radarr": {
                "enabled": True,
                "url": "http://127.0.0.1:7878/",
                "api_key": "updated-radarr-key",
            },
            "binaries": {
                "ffprobe_path": "ffprobe",
                "ffmpeg_path": "ffmpeg",
            },
            "path_mappings": [{"from": r"E:\Temp_Flex_Media", "to": "/media", "use_regex": False}],
        }

        res = client.post("/api/v1/settings", json=update_payload)
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["message"] == "Settings saved and applied successfully"

        # 1. Verify disk persistence
        assert cfg_file.is_file()
        disk_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert disk_data["verification"]["dry_run"] is False
        assert disk_data["verification"]["runtime_delta_threshold_minutes"] == 22
        assert disk_data["verification"]["freeze_ratio_threshold"] == pytest.approx(0.45)
        assert disk_data["radarr"]["url"] == "http://127.0.0.1:7878"
        assert disk_data["radarr"]["api_key"] == "updated-radarr-key"
        assert disk_data["path_mappings"] == [
            {"remote_path": r"E:\Temp_Flex_Media", "local_path": "/media", "use_regex": False}
        ]

        # 2. Verify in-memory hot reload across app.state and Verifier / Remediator
        assert app.state.settings.verification.dry_run is False
        assert app.state.verifier._cfg.dry_run is False
        assert app.state.verifier._cfg.runtime_delta_threshold_minutes == 22
        assert app.state.verifier._cfg.max_concurrent_probes == 4
        assert app.state.remediator._settings.radarr.api_key.get_secret_value() == (
            "updated-radarr-key"
        )

        # 3. Verify header badge updates immediately on /settings and / without restart
        page_after = client.get("/settings")
        assert page_after.status_code == 200
        assert "ACTIVE MODE" in page_after.text

        dashboard_after = client.get("/")
        assert dashboard_after.status_code == 200
        assert "ACTIVE MODE" in dashboard_after.text

        # Toggle dry_run back to True and verify badge reverts to DRY-RUN
        revert_res = client.post("/api/v1/settings", json={"verification": {"dry_run": True}})
        assert revert_res.status_code == 200
        assert "DRY-RUN" in client.get("/settings").text


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {},
        {"verification": {"runtime_delta_threshold_minutes": 0}},
        {"verification": {"runtime_delta_threshold_minutes": -10}},
        {"verification": {"max_concurrent_probes": 0}},
        {"verification": {"freeze_threshold_percent": 150}},
        {"unknown_section": {"foo": "bar"}},
    ],
)
def test_post_api_settings_rejects_invalid_payloads_with_422(
    tmp_path: Path, invalid_payload: dict[str, object]
) -> None:
    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        res = client.post("/api/v1/settings", json=invalid_payload)
        assert res.status_code == 422


def test_connection_tester_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self, url: str, headers: dict[str, str] | None = None):  # noqa: ANN001
        request = httpx.Request("GET", url, headers=headers)
        if "7878" in url and (headers or {}).get("X-Api-Key") == "valid-key":
            return httpx.Response(200, json={"version": "5.14.0.9383"}, request=request)
        return httpx.Response(401, json={"message": "Unauthorized"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        ok_res = client.post(
            "/api/v1/settings/test-connection",
            json={"service": "radarr", "url": "http://localhost:7878", "api_key": "valid-key"},
        )
        assert ok_res.status_code == 200
        assert ok_res.json() == {"status": "ok", "version": "5.14.0.9383"}

        err_res = client.post(
            "/api/v1/settings/test-connection",
            json={"service": "radarr", "url": "http://localhost:7878", "api_key": "bad-key"},
        )
        assert err_res.status_code == 200
        assert err_res.json()["status"] == "error"
        assert "401" in err_res.json()["message"]


def test_app_config_schema_validation() -> None:
    cfg = AppConfig.model_validate(
        {"verification": {"dry_run": False, "runtime_delta_threshold_minutes": 18}}
    )
    assert cfg.verification.dry_run is False
    assert cfg.verification.runtime_delta_threshold_minutes == 18


def test_detect_binaries_endpoint(tmp_path: Path) -> None:
    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        res = client.get("/api/v1/system/detect-binaries")
        assert res.status_code == 200
        data = res.json()
        assert "ffprobe" in data
        assert "ffmpeg" in data
        assert "tesseract" in data
        for name in ("ffprobe", "ffmpeg", "tesseract"):
            assert data[name] is None or isinstance(data[name], str)


def test_browse_fs_endpoint_strict_file_lock(tmp_path: Path) -> None:
    scan_dir = tmp_path / "browse_test"
    scan_dir.mkdir()
    (scan_dir / "subfolder_a").mkdir()
    (scan_dir / "subfolder_b").mkdir()
    (scan_dir / "ffprobe.exe").write_text("dummy binary", encoding="utf-8")
    (scan_dir / "ffmpeg.exe").write_text("dummy binary", encoding="utf-8")
    (scan_dir / "tesseract.exe").write_text("dummy binary", encoding="utf-8")
    (scan_dir / "unrelated_file.txt").write_text("text", encoding="utf-8")
    (scan_dir / "malicious.exe").write_text("bad", encoding="utf-8")

    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        # 1. Target = ffprobe: Only ffprobe.exe should appear in matching_files
        res = client.get(f"/api/v1/system/browse-fs?path={scan_dir}&target_binary=ffprobe")
        assert res.status_code == 200
        data = res.json()
        assert "subfolder_a" in data["directories"]
        assert "subfolder_b" in data["directories"]
        assert data["matching_files"] == ["ffprobe.exe"]

        # 2. Target = ffmpeg: Only ffmpeg.exe should appear in matching_files
        res = client.get(f"/api/v1/system/browse-fs?path={scan_dir}&target_binary=ffmpeg")
        assert res.status_code == 200
        data = res.json()
        assert data["matching_files"] == ["ffmpeg.exe"]

        # 3. Target = tesseract: Only tesseract.exe should appear
        res = client.get(f"/api/v1/system/browse-fs?path={scan_dir}&target_binary=tesseract")
        assert res.status_code == 200
        data = res.json()
        assert data["matching_files"] == ["tesseract.exe"]

        # 4. Target = unknown/empty: No files should match
        res = client.get(f"/api/v1/system/browse-fs?path={scan_dir}&target_binary=unknown")
        assert res.status_code == 200
        data = res.json()
        assert data["matching_files"] == []


def test_browse_fs_fallback_invalid_path(tmp_path: Path) -> None:
    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        res = client.get("/api/v1/system/browse-fs?path=Z:\\NonExistentPath_xyz123")
        assert res.status_code == 200
        data = res.json()
        assert "current_path" in data
        assert "drives" in data
        assert isinstance(data["drives"], list)
        assert len(data["drives"]) >= 1


def test_binary_resolution_hierarchy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.services.binary_resolver import resolve_binary

    custom_exe = tmp_path / "custom_ffprobe.exe"
    custom_exe.write_text("binary", encoding="utf-8")

    path_exe = tmp_path / "ffprobe.exe"
    path_exe.write_text("binary", encoding="utf-8")

    # Priority 1: User-configured manual path wins
    resolved = resolve_binary("ffprobe", custom_path=str(custom_exe))
    assert resolved == custom_exe.resolve()

    # Priority 2: When custom path is invalid, falls back to system PATH via shutil.which
    monkeypatch.setattr("shutil.which", lambda name: str(path_exe) if name == "ffprobe" else None)
    resolved_fallback = resolve_binary("ffprobe", custom_path=str(tmp_path / "nonexistent.exe"))
    assert resolved_fallback == path_exe.resolve()

    # Priority 3: When neither custom nor PATH exists, falls back to common directories
    monkeypatch.setattr("shutil.which", lambda name: None)
    fake_common = tmp_path / "jellyfin" / "ffprobe.exe"
    fake_common.parent.mkdir()
    fake_common.write_text("binary", encoding="utf-8")
    monkeypatch.setattr(
        "src.services.binary_resolver.COMMON_PATHS", {"ffprobe": [str(fake_common)]}
    )
    resolved_common = resolve_binary("ffprobe")
    assert resolved_common == fake_common.resolve()


def test_path_mapping_sanitization_and_aliases() -> None:
    from src.config import PathMapping

    # Trailing slash / backslash stripping
    m1 = PathMapping.model_validate(
        {"remote_path": r"E:\Temp_Flex_Media\\", "local_path": "/media/"}
    )
    assert m1.remote_path == r"E:\Temp_Flex_Media"
    assert m1.local_path == "/media"
    assert m1.from_path == r"E:\Temp_Flex_Media"
    assert m1.to_path == "/media"

    # From / to aliases and regex
    m2 = PathMapping.model_validate(
        {"from_path": r"D:\Downloads\\", "to_path": "/downloads/", "regex": True}
    )
    assert m2.remote_path == r"D:\Downloads"
    assert m2.local_path == "/downloads"
    assert m2.use_regex is True

    # Root drive preservation
    m3 = PathMapping.model_validate({"from": "C:\\", "to": "/"})
    assert m3.remote_path == "C:\\"
    assert m3.local_path == "/"


def test_settings_save_with_trailing_slashes_and_relaxed_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_file = tmp_path / "config.yaml"
    monkeypatch.setenv("VERIFYARR_CONFIG", str(cfg_file))

    app = create_app(_make_test_settings(tmp_path))
    with TestClient(app) as client:
        payload = {
            "verification": {"dry_run": True},
            "binaries": {
                "ffprobe_path": "C:\\Program Files\\Custom\\ffprobe.exe",
                "ffmpeg_path": "arbitrary_custom_binary",
                "tesseract_path": "C:\\Program Files\\Tesseract-OCR\\nonexistent.exe",
            },
            "path_mappings": [
                {"from": r"E:\Temp_Flex_Media\\", "to": "/media/", "use_regex": False}
            ],
        }
        res = client.post("/api/v1/settings", json=payload)
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        # Verify saved in config.yaml without trailing slashes
        disk_data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert disk_data["path_mappings"] == [
            {"local_path": "/media", "remote_path": r"E:\Temp_Flex_Media", "use_regex": False}
        ]
        assert disk_data["binaries"]["ffprobe_path"] == "C:\\Program Files\\Custom\\ffprobe.exe"
        assert disk_data["binaries"]["ffmpeg_path"] == "arbitrary_custom_binary"
        assert (
            disk_data["binaries"]["tesseract_path"]
            == "C:\\Program Files\\Tesseract-OCR\\nonexistent.exe"
        )
