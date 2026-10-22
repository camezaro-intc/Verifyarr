"""Webhooks router for Servarr integration and Grab event interception."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from src.config import Settings
from src.models.servarr import ServarrWebhookPayload, extract_fallback_jobs

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/v1/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Ingest webhooks from Radarr/Sonarr (Download, Upgrade, and Grab events)."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    raw_movie = body.get("movie")
    raw_series = body.get("series")
    movie_raw: dict[str, Any] = raw_movie if isinstance(raw_movie, dict) else {}
    series_raw: dict[str, Any] = raw_series if isinstance(raw_series, dict) else {}
    logger.info(
        "Incoming Webhook: eventType='%s', title='%s'",
        body.get("eventType"),
        movie_raw.get("title") or series_raw.get("title") or body.get("title"),
    )
    logger.debug("Raw webhook payload: %s", body)

    event_type = str(body.get("eventType", "")).strip().lower()

    # Intercept Test events before validation so the "Test" button always succeeds.
    if event_type == "test":
        logger.info("Received webhook test event")
        return JSONResponse({"status": "ok", "message": "Verifyarr webhook test successful"})

    active: Settings = request.app.state.settings

    # Intercept Grab events for Tier 0 Pre-Download Inspection
    if event_type == "grab":
        if active.qbittorrent.enabled and active.qbittorrent.inspect_on_grab:
            checker = request.app.state.pre_download_checker
            background_tasks.add_task(checker.inspect_grab_event, body)
            logger.info("Scheduled Tier 0 pre-download inspection for Grab event")
            return JSONResponse({"status": "ok", "message": "Pre-download inspection initiated"})
        logger.info("Ignoring Grab event: qBittorrent pre-download inspection is disabled")
        return JSONResponse(
            {"status": "ignored", "message": "qBittorrent pre-download inspection is disabled"}
        )

    try:
        payload = ServarrWebhookPayload.model_validate(body)
    except ValidationError as exc:
        logger.error(
            "Webhook Pydantic validation error (attempting fallback extraction): %s",
            exc.errors(include_url=False),
        )
        fallback_jobs = extract_fallback_jobs(body)
        if not fallback_jobs:
            raise HTTPException(
                status_code=422, detail=exc.errors(include_url=False, include_context=False)
            ) from exc
        fb_app = fallback_jobs[0].app
        if not getattr(active, fb_app).enabled:
            return JSONResponse(
                {"status": "ignored", "message": f"{fb_app} integration is disabled"}
            )
        queued = request.app.state.verifier.submit(fallback_jobs)
        logger.info(
            "Queued %d file(s) via fallback extractor from %s event",
            queued,
            body.get("eventType"),
        )
        return JSONResponse({"status": "accepted", "queued": queued}, status_code=202)

    if not payload.is_import_event:
        logger.info("Ignoring non-import webhook event '%s'", payload.event_type)
        return JSONResponse(
            {"status": "ignored", "message": f"Event {payload.event_type!r} is not verified"}
        )

    app_name = payload.app
    if app_name is None:
        raise HTTPException(status_code=400, detail="Payload has neither movie nor series")
    if not getattr(active, app_name).enabled:
        logger.info("Ignoring webhook because %s integration is disabled", app_name)
        return JSONResponse({"status": "ignored", "message": f"{app_name} integration is disabled"})

    jobs = payload.to_jobs() or extract_fallback_jobs(body)
    if not jobs:
        entity = payload.movie or payload.series
        if entity is not None and entity.id is None:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["movieFile", "path"],
                        "msg": "Field required: media file path missing",
                        "type": "missing",
                    }
                ],
            )
        logger.info("Webhook payload contains no media file to verify")
        return JSONResponse(
            {"status": "ignored", "message": "Payload contains no imported media file"}
        )

    # Verification runs in the background; Servarr must not wait on ffmpeg / season packs.
    queued = request.app.state.verifier.submit(jobs)
    logger.info("Queued %d file(s) from %s %s event", queued, app_name, payload.event_type)
    return JSONResponse({"status": "accepted", "queued": queued}, status_code=202)
