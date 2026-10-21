"""Servarr REST API client used to remediate rejected releases (delete, blocklist, re-search)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.config import ArrConfig, Settings
from src.models.servarr import AppName

logger = logging.getLogger(__name__)

_GRABBED_EVENT_TYPE = 1  # Servarr history eventType: grabbed


class RemediationError(RuntimeError):
    """A Servarr API call failed or the application is not configured."""


class Remediator:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ------------------------------------------------------------------

    def _config(self, app: AppName) -> ArrConfig:
        config = self._settings.radarr if app == "radarr" else self._settings.sonarr
        if not config.enabled:
            raise RemediationError(f"{app} integration is disabled in config")
        if not config.url or not config.api_key.get_secret_value():
            raise RemediationError(f"{app} url/api_key is not configured")
        return config

    async def _request(
        self,
        app: AppName,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        config = self._config(app)
        url = f"{config.url}{path}"
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers={"X-Api-Key": config.api_key.get_secret_value()},
            )
        except httpx.HTTPError as exc:
            raise RemediationError(f"{app} {method} {path} failed: {exc!r}") from exc
        if response.status_code >= 400:
            raise RemediationError(
                f"{app} {method} {path} returned HTTP {response.status_code}: {response.text[:200]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    # -- metadata ------------------------------------------------------------------

    async def get_media_metadata(self, app: AppName, entity_id: int) -> dict[str, Any]:
        """Fetch the movie / series record (runtime, originalLanguage) from the Servarr API."""
        path = f"/api/v3/movie/{entity_id}" if app == "radarr" else f"/api/v3/series/{entity_id}"
        data = await self._request(app, "GET", path)
        return data if isinstance(data, dict) else {}

    # -- remediation actions -------------------------------------------------------

    async def delete_file(self, app: AppName, file_id: int) -> str:
        """``DELETE /api/v3/moviefile/{id}`` (Radarr) or ``/api/v3/episodefile/{id}`` (Sonarr)."""
        resource = "moviefile" if app == "radarr" else "episodefile"
        await self._request(app, "DELETE", f"/api/v3/{resource}/{file_id}")
        return f"Deleted {resource} {file_id}"

    async def blocklist_release(
        self,
        app: AppName,
        release_title: str,
        *,
        entity_id: int,
        download_id: str | None = None,
        indexer: str | None = None,
        size: int | None = None,
        episode_ids: list[int] | None = None,
    ) -> str:
        """Blocklist the release that produced the file.

        Servarr's supported way to blocklist an already-imported release is to mark its
        *grabbed* history record as failed (``POST /api/v3/history/failed/{id}``). When no
        matching history record exists, fall back to ``POST /api/v3/blocklist`` which newer
        or patched builds may accept.
        """
        history_id = await self._find_grabbed_history_id(app, release_title, entity_id, download_id)
        if history_id is not None:
            await self._request(app, "POST", f"/api/v3/history/failed/{history_id}")
            return f"Blocklisted release via history record {history_id}"

        payload: dict[str, Any] = {
            "sourceTitle": release_title,
            "indexer": indexer,
            "size": size,
            "downloadId": download_id,
            "message": "Rejected by Verifyarr",
        }
        if app == "radarr":
            payload["movieId"] = entity_id
        else:
            payload["seriesId"] = entity_id
            payload["episodeIds"] = episode_ids or []
        await self._request(app, "POST", "/api/v3/blocklist", json=payload)
        return "Blocklisted release via /api/v3/blocklist"

    async def _find_grabbed_history_id(
        self, app: AppName, release_title: str, entity_id: int, download_id: str | None
    ) -> int | None:
        if app == "radarr":
            path, params = "/api/v3/history/movie", {
                "movieId": entity_id,
                "eventType": _GRABBED_EVENT_TYPE,
            }
        else:
            path, params = "/api/v3/history/series", {
                "seriesId": entity_id,
                "eventType": _GRABBED_EVENT_TYPE,
            }
        try:
            data = await self._request(app, "GET", path, params=params)
        except RemediationError as exc:
            logger.warning("Could not look up grabbed history: %s", exc)
            return None

        records = data.get("records", []) if isinstance(data, dict) else (data or [])
        grabbed = [
            r
            for r in records
            if str(r.get("eventType", "grabbed")).lower() in ("grabbed", str(_GRABBED_EVENT_TYPE))
        ]
        if download_id:
            for record in grabbed:
                if str(record.get("downloadId", "")).lower() == download_id.lower():
                    return int(record["id"])
        for record in grabbed:
            if record.get("sourceTitle") == release_title:
                return int(record["id"])
        return None

    async def trigger_search(
        self, app: AppName, entity_id: int, *, episode_ids: list[int] | None = None
    ) -> str:
        """Queue a replacement search via ``POST /api/v3/command``.

        Radarr uses ``MoviesSearch``. Sonarr searches only the affected episodes when their
        ids are known (``EpisodeSearch``) rather than re-searching the whole series.
        """
        if app == "radarr":
            body: dict[str, Any] = {"name": "MoviesSearch", "movieIds": [entity_id]}
        elif episode_ids:
            body = {"name": "EpisodeSearch", "episodeIds": episode_ids}
        else:
            body = {"name": "SeriesSearch", "seriesId": entity_id}
        await self._request(app, "POST", "/api/v3/command", json=body)
        return f"Triggered {body['name']}"
