"""System management router: Live logging viewer, log download, and diagnostics."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["System"])

DEFAULT_LOG_PATH = Path(r"C:\Verifyarr\data\verifyarr.log")
LOG_REGEX = re.compile(r"^(\d{4}-\d{2}-\d{2}T[\d:\.\-+]+)\s+([A-Z]+)\s+([\w\.\-]+):\s+(.*)$")


def resolve_log_path() -> Path:
    """Resolve the active log file across Windows service, Docker, and dev environments."""
    env_path = os.getenv("VERIFYARR_LOG_PATH")
    if env_path is not None:
        return Path(env_path)

    if DEFAULT_LOG_PATH.exists():
        return DEFAULT_LOG_PATH

    local_data = Path("data") / "verifyarr.log"
    if local_data.exists():
        return local_data

    return DEFAULT_LOG_PATH


def _read_tail_lines(log_path: Path, lines: int) -> list[str]:
    """Synchronously read the tail of the log file in a worker thread."""
    with open(log_path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return all_lines[-lines:]


@router.get("/logs")
def get_logs(
    lines: int = Query(100, ge=10, le=1000),
    level: str | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """Retrieve and parse the most recent log entries from verifyarr.log."""
    log_path = resolve_log_path()
    if not log_path.exists():
        return {"lines": [], "total": 0, "file_path": str(log_path), "file_exists": False}

    try:
        tail = _read_tail_lines(log_path, lines)
        parsed: list[dict[str, str]] = []
        target_level = level.strip().upper() if level and level.strip().upper() != "ALL" else None
        target_search = search.strip().lower() if search and search.strip() else None

        for line in tail:
            stripped = line.strip()
            if not stripped:
                continue

            match = LOG_REGEX.match(stripped)
            if match:
                lvl = match.group(2)
                if target_level and lvl != target_level:
                    continue
                if target_search and target_search not in stripped.lower():
                    continue

                parsed.append(
                    {
                        "timestamp": match.group(1),
                        "level": lvl,
                        "logger": match.group(3),
                        "message": match.group(4),
                        "raw": stripped,
                    }
                )
            else:
                if not target_level:
                    if target_search and target_search not in stripped.lower():
                        continue
                    parsed.append(
                        {
                            "timestamp": "",
                            "level": "INFO",
                            "logger": "",
                            "message": stripped,
                            "raw": stripped,
                        }
                    )

        return {
            "lines": parsed,
            "total": len(parsed),
            "file_path": str(log_path),
            "file_exists": True,
        }
    except Exception as exc:
        logger.error("Failed reading log file %s: %s", log_path, exc)
        raise HTTPException(status_code=500, detail=f"Error reading log file: {exc}") from exc


@router.get("/logs/download")
async def download_logs() -> FileResponse:
    """Download the full verifyarr.log file."""
    log_path = resolve_log_path()
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"Log file not found at {log_path}")

    return FileResponse(
        path=str(log_path),
        filename="verifyarr.log",
        media_type="text/plain",
    )


@router.get("/history/{history_id}/correlated-logs")
async def get_system_correlated_logs(history_id: int, request: Request) -> dict[str, Any]:
    """Retrieve chronologically aggregated and correlated logs across services
    for a history entry.
    """
    from src.models.history import HistoryStore
    from src.services.log_aggregator import LogAggregator

    history_store: HistoryStore | None = getattr(request.app.state, "history", None)
    if history_store is None:
        raise HTTPException(status_code=500, detail="History store is not initialized")

    record = await history_store.get(history_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"History item {history_id} not found")

    settings = getattr(request.app.state, "settings", None)
    aggregator = LogAggregator(settings=settings, history_store=history_store)
    return await aggregator.aggregate_for_record(record)


class UpdateApplyPayload(BaseModel):
    download_url: str


@router.get("/version")
def get_system_version() -> dict[str, str]:
    """Return local active Verifyarr version without querying external GitHub API."""
    from src import __version__

    return {"version": __version__}


@router.get("/update/check")
async def check_update() -> dict[str, Any]:
    """Check GitHub releases for available Verifyarr updates."""
    from src.services.updater import UpdateManager

    updater = UpdateManager()
    return await updater.check_for_updates()


@router.post("/update/apply")
async def apply_system_update(
    payload: UpdateApplyPayload,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Trigger background update download, installation, and service restart."""
    from src.services.updater import UpdateManager

    if not payload.download_url or not payload.download_url.strip():
        raise HTTPException(status_code=400, detail="Missing or empty download_url")

    updater = UpdateManager()
    background_tasks.add_task(updater.apply_update, payload.download_url)
    return {"status": "restarting"}


