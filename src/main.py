"""Verifyarr FastAPI application: webhook ingestion, dashboard and force-approve endpoint."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src import __version__
from src.config import Settings, load_settings
from src.models.history import HistoryStore, VerificationStatus
from src.routers.settings import router as settings_router
from src.routers.webhooks import router as webhooks_router
from src.services.notifier import Notifier
from src.services.pre_download_checker import PreDownloadInspector
from src.services.remediator import Remediator
from src.services.verifier import Verifier

logger = logging.getLogger("verifyarr")


def _resolve_template_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for candidate in (meipass / "src" / "templates", meipass / "templates"):
            if candidate.is_dir():
                return candidate
    return Path(__file__).parent / "templates"


TEMPLATE_DIR = _resolve_template_dir()
DASHBOARD_LIMIT = 50


class _IsoFormatter(logging.Formatter):
    """Log formatter emitting ISO-8601 timestamps with UTC offset."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


def configure_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_IsoFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if level in ("debug", "trace") else getattr(logging, level.upper()))


configure_logging("info")


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        try:
            active = settings or load_settings()
        except Exception as exc:
            logger.error("Failed to load settings in lifespan: %s. Using defaults.", exc)
            active = settings or Settings()
        configure_logging(active.server.log_level)
        logger.info("Starting Verifyarr %s", __version__)

        history = HistoryStore(active.storage.database_path)
        await history.open()
        remediator = Remediator(active)
        notifier = Notifier(active)
        pre_download_checker = PreDownloadInspector(active, history, remediator, notifier)
        verifier = Verifier(active, history, remediator, notifier)
        verifier.log_environment()
        _warn_on_placeholder_keys(active)

        app.state.settings = active
        app.state.history = history
        app.state.remediator = remediator
        app.state.notifier = notifier
        app.state.pre_download_checker = pre_download_checker
        app.state.verifier = verifier
        try:
            yield
        finally:
            await pre_download_checker.aclose()
            await verifier.shutdown()
            await notifier.aclose()
            await remediator.aclose()
            await history.close()

    app = FastAPI(title="Verifyarr", version=__version__, lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    app.state.templates = templates
    app.include_router(settings_router)
    app.include_router(webhooks_router)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "dry_run": request.app.state.settings.verification.dry_run,
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        entries = await request.app.state.history.recent(DASHBOARD_LIMIT)
        counts = {status.value: 0 for status in VerificationStatus}
        for entry in entries:
            counts[entry.status.value] += 1
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "entries": entries,
                "counts": counts,
                "dry_run": request.app.state.settings.verification.dry_run,
                "version": __version__,
                "limit": DASHBOARD_LIMIT,
                "active_page": "dashboard",
            },
        )

    @app.post("/api/v1/whitelist/{item_id}")
    async def whitelist(item_id: int, request: Request) -> dict[str, Any]:
        history: HistoryStore = request.app.state.history
        entry = await history.get(item_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"History item {item_id} not found")
        if entry.whitelisted:
            return {"status": "ok", "message": "Already whitelisted", "jellyfin_refreshed": False}
        if not entry.can_force_approve:
            raise HTTPException(
                status_code=409, detail=f"Item is {entry.status.value}; nothing to override"
            )

        await history.whitelist(item_id)
        refreshed = await request.app.state.notifier.refresh_jellyfin(immediate=True)
        logger.info(
            "Force-approved history item %d (%s); Jellyfin refreshed=%s",
            item_id,
            entry.release_name,
            refreshed,
        )
        return {"status": "ok", "message": "Whitelisted", "jellyfin_refreshed": refreshed}

    return app


def _warn_on_placeholder_keys(settings: Settings) -> None:
    for name in ("radarr", "sonarr", "jellyfin"):
        config = getattr(settings, name)
        if config.enabled and config.api_key.get_secret_value().startswith("YOUR_"):
            logger.warning("%s is enabled but api_key is still the placeholder value", name)


app = create_app()


def run() -> None:
    try:
        settings = load_settings()
    except Exception as exc:
        logger.error("Failed to load settings: %s. Using defaults.", exc)
        settings = Settings()

    configure_logging(settings.server.log_level)
    host = getattr(settings.server, "host", "0.0.0.0")
    port = getattr(settings.server, "port", 7979)
    log_level = getattr(settings.server, "log_level", "info")

    # Pass the app object directly to support frozen PyInstaller binaries
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        log_config=None,
    )


if __name__ == "__main__":
    run()
