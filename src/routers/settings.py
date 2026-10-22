"""Settings management router: Web UI page, REST API, atomic persistence, and connection tester."""

from __future__ import annotations

import copy
import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src import __version__
from src.config import (
    AppConfig,
    BinariesConfig,
    Settings,
    VerificationConfig,
    get_config_path,
    save_config,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_VERIFICATION_KEYS = frozenset(VerificationConfig.model_fields.keys()) | {
    "freeze_threshold_percent"
}
_BINARIES_KEYS = frozenset(BinariesConfig.model_fields.keys())
_SECTION_KEYS = frozenset(AppConfig.model_fields.keys())


class ConnectionTestRequest(BaseModel):
    """Payload for ``POST /api/v1/settings/test-connection``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    service: Literal["radarr", "sonarr", "jellyfin"] = Field(alias="service")
    url: str = ""
    api_key: str = ""


class QBittorrentTestRequest(BaseModel):
    """Payload for ``POST /api/v1/settings/test-qbittorrent``."""

    model_config = ConfigDict(extra="ignore")

    url: str = "http://localhost:8080"
    username: str = "admin"
    password: str = ""


def _merge_settings_payload(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge a full, partial, or flat incoming settings payload into a copy of ``current``.

    Unknown keys are left in place so ``AppConfig.model_validate`` (``extra="forbid"``) raises a
    standard Pydantic ``ValidationError`` that FastAPI converts into HTTP 422.
    """
    if not incoming:
        raise ValueError("Settings payload cannot be empty")

    merged = copy.deepcopy(current)
    for key, value in incoming.items():
        if key in _SECTION_KEYS:
            if key == "path_mappings":
                merged["path_mappings"] = value
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        elif key in _VERIFICATION_KEYS:
            merged.setdefault("verification", {})[key] = value
        elif key in _BINARIES_KEYS:
            merged.setdefault("binaries", {})[key] = value
        else:
            # Preserve unknown top-level key so AppConfig raises a 422 validation error.
            merged[key] = value
    return merged


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the interactive Settings page."""
    active: Settings = request.app.state.settings
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": active.to_dict(include_computed=True),
            "config_path": str(get_config_path().resolve()),
            "dry_run": active.verification.dry_run,
            "version": __version__,
            "active_page": "settings",
        },
    )


@router.get("/api/v1/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    """Return the active configuration as JSON (including API keys for the local admin)."""
    active: Settings = request.app.state.settings
    return active.to_dict(include_computed=True)


@router.post("/api/v1/settings")
async def update_settings(request: Request) -> JSONResponse:
    """Validate, atomically persist to ``config.yaml``, and hot-reload settings in memory."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body must be valid JSON") from exc

    if not isinstance(body, dict) or not body:
        raise RequestValidationError(
            [
                {
                    "loc": ("body",),
                    "msg": "Settings payload must be a non-empty JSON object",
                    "type": "value_error",
                }
            ]
        )

    active: Settings = request.app.state.settings
    try:
        merged_dict = _merge_settings_payload(active.to_dict(include_computed=False), body)
        validated_app_cfg = AppConfig.model_validate(merged_dict)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc

    new_settings = validated_app_cfg.to_settings()

    # 1. Atomically persist to config.yaml on disk
    try:
        saved_path = save_config(new_settings)
    except OSError as exc:
        logger.error("Failed to save configuration to disk: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to write config file: {exc}") from exc

    # 2. Hot-reload active configuration in memory across all services
    active.update_in_place(new_settings)
    request.app.state.settings = active
    if hasattr(request.app.state, "verifier"):
        request.app.state.verifier.reload_settings(active)
    if hasattr(request.app.state, "remediator"):
        request.app.state.remediator.reload_settings(active)
    if hasattr(request.app.state, "notifier"):
        request.app.state.notifier.reload_settings(active)
    if hasattr(request.app.state, "pre_download_checker"):
        request.app.state.pre_download_checker.reload_settings(active)

    logger.info(
        "Settings updated and hot-reloaded via Web UI (dry_run=%s, path=%s)",
        active.verification.dry_run,
        saved_path,
    )
    return JSONResponse(
        {
            "status": "ok",
            "message": "Settings saved and applied successfully",
            "settings": active.to_dict(include_computed=True),
        }
    )


@router.post("/api/v1/settings/test-connection")
async def test_connection(request: Request) -> JSONResponse:
    """Test connectivity and API key validity against Radarr, Sonarr, or Jellyfin."""
    try:
        raw = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body must be valid JSON") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    # Allow either "service" or "type" in payload
    if "type" in raw and "service" not in raw:
        raw = {**raw, "service": raw["type"]}

    try:
        payload = ConnectionTestRequest.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc

    base_url = payload.url.strip().rstrip("/")
    api_key = payload.api_key.strip()
    if not base_url:
        return JSONResponse({"status": "error", "message": "Base URL is required"}, status_code=200)
    if not api_key:
        return JSONResponse({"status": "error", "message": "API Key is required"}, status_code=200)

    if payload.service in ("radarr", "sonarr"):
        endpoint = f"{base_url}/api/v3/system/status"
        headers = {"X-Api-Key": api_key}
    else:
        endpoint = f"{base_url}/System/Info"
        headers = {"X-Emby-Token": api_key}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0)) as client:
            response = await client.get(endpoint, headers=headers)
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"status": "error", "message": f"Connection failed: {exc}"}, status_code=200
        )

    if response.status_code in (401, 403):
        return JSONResponse(
            {"status": "error", "message": f"Authentication failed (HTTP {response.status_code})"},
            status_code=200,
        )
    if response.status_code >= 400:
        return JSONResponse(
            {
                "status": "error",
                "message": f"Server returned HTTP {response.status_code}",
            },
            status_code=200,
        )

    try:
        data = response.json()
    except ValueError:
        data = {}

    version = (
        str(data.get("version") or data.get("Version") or "unknown")
        if isinstance(data, dict)
        else "unknown"
    )
    return JSONResponse({"status": "ok", "version": version})


@router.post("/api/v1/settings/test-qbittorrent")
async def test_qbittorrent(request: Request) -> JSONResponse:
    """Test connectivity and credentials against the qBittorrent Web API."""
    try:
        raw = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body must be valid JSON") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    try:
        payload = QBittorrentTestRequest.model_validate(raw)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False)) from exc

    base_url = payload.url.strip().rstrip("/")
    if not base_url:
        return JSONResponse(
            {"status": "error", "message": "qBittorrent URL is required"}, status_code=200
        )

    from src.services.qbittorrent import QBittorrentClient

    client = QBittorrentClient(
        url=base_url,
        username=payload.username.strip(),
        password=payload.password,
        timeout=6.0,
    )
    try:
        ok, message = await client.test_connection()
        if ok:
            return JSONResponse({"status": "ok", "message": message}, status_code=200)
        return JSONResponse({"status": "error", "message": message}, status_code=200)
    finally:
        await client.aclose()


def _get_system_drives() -> list[str]:
    """Return available root drives (e.g. ['C:\\', 'E:\\'] on Windows, ['/'] on POSIX)."""
    if sys.platform == "win32":
        import string

        drives = []
        for letter in string.ascii_uppercase:
            candidate = f"{letter}:\\"
            if os.path.exists(candidate):
                drives.append(candidate)
        return drives or ["C:\\"]
    return ["/"]


@router.get("/api/v1/system/detect-binaries")
def detect_binaries() -> dict[str, str | None]:
    """Auto-detect standard locations and system PATH for ffprobe, ffmpeg, and tesseract."""
    from src.config import normalize_path
    from src.services.binary_resolver import resolve_binary

    result: dict[str, str | None] = {}
    for name in ("ffprobe", "ffmpeg", "tesseract"):
        resolved = resolve_binary(name)
        result[name] = normalize_path(str(resolved)) if resolved else None
    return result


@router.get("/api/v1/system/browse-fs")
def browse_fs(
    path: str = "",
    target_binary: str = "",
) -> dict[str, Any]:
    """Browse the filesystem with a strict lock that only exposes files matching target_binary."""
    drives = _get_system_drives()
    default_root = drives[0] if drives else "/"

    if not path or not path.strip():
        current_dir = Path(default_root).resolve()
    else:
        current_dir = Path(path.strip()).resolve()

    if not current_dir.exists() or not current_dir.is_dir():
        current_dir = Path(default_root).resolve()

    parent_path: str | None = str(current_dir.parent) if current_dir.parent != current_dir else None

    directories: list[str] = []
    matching_files: list[str] = []

    target_clean = target_binary.strip().lower()
    expected_names = {target_clean}
    if target_clean.endswith(".exe"):
        expected_names.add(target_clean[:-4])
    else:
        expected_names.add(f"{target_clean}.exe")

    try:
        with os.scandir(current_dir) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        entry_lower = entry.name.lower()
                        # Strict Lock Rule: only matching executables are allowed
                        if target_clean and entry_lower in expected_names:
                            matching_files.append(entry.name)
                except OSError:
                    continue
    except (PermissionError, OSError) as exc:
        logger.debug("Failed scanning directory %s: %s", current_dir, exc)

    directories.sort(key=str.lower)
    matching_files.sort(key=str.lower)

    return {
        "current_path": str(current_dir),
        "parent_path": parent_path,
        "drives": drives,
        "directories": directories,
        "matching_files": matching_files,
    }
