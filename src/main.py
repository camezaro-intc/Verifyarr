"""Verifyarr FastAPI application: webhook ingestion, dashboard and force-approve endpoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from src import __version__
from src.config import Settings, load_settings
from src.models.history import HistoryStore, VerificationStatus
from src.models.servarr import ServarrWebhookPayload
from src.services.notifier import Notifier
from src.services.remediator import Remediator
from src.services.verifier import Verifier

logger = logging.getLogger("verifyarr")

TEMPLATE_DIR = Path(__file__).parent / "templates"
DASHBOARD_LIMIT = 50


class _IsoFormatter(logging.Formatter):
    """Log formatter emitting ISO-8601 timestamps with UTC offset."""

    def formatTime(
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_IsoFormatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if level in ("debug", "trace") else getattr(logging, level.upper()))


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        active = settings or load_settings()
        configure_logging(active.server.log_level)
        logger.info("Starting Verifyarr %s", __version__)

        history = HistoryStore(active.storage.database_path)
        await history.open()
        remediator = Remediator(active)
        notifier = Notifier(active)
        verifier = Verifier(active, history, remediator, notifier)
        verifier.log_environment()
        _warn_on_placeholder_keys(active)

        app.state.settings = active
        app.state.history = history
        app.state.notifier = notifier
        app.state.verifier = verifier
        try:
            yield
        finally:
            await verifier.shutdown()
            await notifier.aclose()
            await remediator.aclose()
            await history.close()

    app = FastAPI(title="Verifyarr", version=__version__, lifespan=lifespan)
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "dry_run": request.app.state.settings.verification.dry_run,
        }

    @app.post("/api/v1/webhook")
    async def webhook(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Body must be valid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        # Intercept Test events before validation so the "Test" button always succeeds.
        if str(body.get("eventType", "")).lower() == "test":
            logger.info("Received webhook test event")
            return JSONResponse({"status": "ok", "message": "Verifyarr webhook test successful"})

        try:
            payload = ServarrWebhookPayload.model_validate(body)
        except ValidationError as exc:
            logger.warning("Rejected malformed webhook payload: %s", exc.errors(include_url=False))
            raise HTTPException(
                status_code=422, detail=exc.errors(include_url=False, include_context=False)
            ) from exc

        if not payload.is_import_event:
            return JSONResponse(
                {"status": "ignored", "message": f"Event {payload.event_type!r} is not verified"}
            )

        active: Settings = request.app.state.settings
        app_name = payload.app
        if app_name is None:
            raise HTTPException(status_code=400, detail="Payload has neither movie nor series")
        if not getattr(active, app_name).enabled:
            return JSONResponse(
                {"status": "ignored", "message": f"{app_name} integration is disabled"}
            )

        jobs = payload.to_jobs()
        if not jobs:
            return JSONResponse(
                {"status": "ignored", "message": "Payload contains no imported media file"}
            )

        # Verification runs in the background; Servarr must not wait on ffmpeg / season packs.
        queued = request.app.state.verifier.submit(jobs)
        logger.info("Queued %d file(s) from %s %s event", queued, app_name, payload.event_type)
        return JSONResponse({"status": "accepted", "queued": queued}, status_code=202)

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
    settings = load_settings()
    configure_logging(settings.server.log_level)
    # log_config=None keeps uvicorn's loggers on our ISO-timestamp root handler.
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        log_config=None,
    )


if __name__ == "__main__":
    run()
