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

    def reload_settings(self, settings: Settings) -> None:
        """Update the active settings reference in memory."""
        self._settings = settings

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- plumbing ------------------------------------------------------------------

    def _config(self, app: AppName) -> ArrConfig:
        config = self._settings.radarr if app == "radarr" else self._settings.sonarr
        if not config.enabled:
            raise RemediationError(f"{app} integration is disabled in config")
        api_key_str = (
            config.api_key.get_secret_value()
            if hasattr(config.api_key, "get_secret_value")
            else str(config.api_key)
        )
        if not config.url or not api_key_str:
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
        api_key_str = (
            config.api_key.get_secret_value()
            if hasattr(config.api_key, "get_secret_value")
            else str(config.api_key)
        )
        url = f"{config.url}{path}"
        try:
            response = await self._client.request(
                method,
                url,
                params=params,
                json=json,
                headers={"X-Api-Key": api_key_str},
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

    async def remove_from_queue_and_blocklist(
        self,
        app: AppName,
        download_id: str,
        *,
        release_title: str = "",
        entity_id: int | None = None,
        indexer: str | None = None,
        size: int | None = None,
        episode_ids: list[int] | None = None,
    ) -> str:
        """Remove a release from the active Servarr queue and blocklist it.

        1. Query the active queue (GET /api/v3/queue?includeUnknownSeriesItems=true).
        2. Search items for item.get('downloadId', '').lower() == download_hash.lower().
        3. If found: DELETE /api/v3/queue/{id}
           with params blocklist=true, skipRedownload=false, removeFromClient=false.
        4. If not found in queue: log a debug entry and fall back to
           POST /api/v3/history/failed/{id}.
        """
        queue_params = (
            {"includeUnknownSeriesItems": "true"}
            if app == "sonarr"
            else {"includeUnknownMovieItems": "true"}
        )
        try:
            data = await self._request(app, "GET", "/api/v3/queue", params=queue_params)
            records = (
                data.get("records", [])
                if isinstance(data, dict)
                else (data if isinstance(data, list) else [])
            )
            clean_hash = download_id.strip().lower()
            matched_item = None
            for item in records:
                if (
                    isinstance(item, dict)
                    and str(item.get("downloadId", "")).strip().lower() == clean_hash
                ):
                    matched_item = item
                    break

            if matched_item and matched_item.get("id") is not None:
                queue_id = int(matched_item["id"])
                await self._request(
                    app,
                    "DELETE",
                    f"/api/v3/queue/{queue_id}",
                    params={
                        "blocklist": "true",
                        "skipRedownload": "false",
                        "removeFromClient": "false",
                    },
                )
                logger.info(
                    "Removed queue item %d and blocklisted %s in %s",
                    queue_id,
                    release_title or download_id,
                    app,
                )
                return f"Removed queue item {queue_id} and blocklisted release"
        except RemediationError as exc:
            logger.debug(
                "Could not inspect active %s queue for downloadId %s: %s",
                app,
                download_id,
                exc,
            )

        logger.debug(
            "Download ID '%s' not found in active %s queue; falling back to history",
            download_id,
            app,
        )
        history_id = await self._find_grabbed_history_id(
            app, release_title, entity_id, download_id
        )
        if history_id is not None:
            await self._request(app, "POST", f"/api/v3/history/failed/{history_id}")
            return f"Blocklisted release via history record {history_id}"

        if app == "radarr":
            try:
                payload: dict[str, Any] = {
                    "sourceTitle": release_title,
                    "indexer": indexer,
                    "size": size,
                    "downloadId": download_id,
                    "message": "Rejected by Verifyarr",
                    "movieId": entity_id,
                }
                await self._request(app, "POST", "/api/v3/blocklist", json=payload)
                return "Blocklisted release via /api/v3/blocklist"
            except RemediationError as exc:
                logger.warning("Radarr /api/v3/blocklist fallback failed: %s", exc)

        logger.debug(
            "No active queue item or grabbed history record found to blocklist for %s (%s)",
            release_title,
            download_id,
        )
        return f"No queue or history record found to blocklist for {release_title}"

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
        """Blocklist the release that produced the file."""
        if download_id:
            return await self.remove_from_queue_and_blocklist(
                app,
                download_id,
                release_title=release_title,
                entity_id=entity_id,
                indexer=indexer,
                size=size,
                episode_ids=episode_ids,
            )

        history_id = await self._find_grabbed_history_id(
            app, release_title, entity_id, download_id
        )
        if history_id is not None:
            await self._request(app, "POST", f"/api/v3/history/failed/{history_id}")
            return f"Blocklisted release via history record {history_id}"

        if app == "radarr":
            payload: dict[str, Any] = {
                "sourceTitle": release_title,
                "indexer": indexer,
                "size": size,
                "downloadId": download_id,
                "message": "Rejected by Verifyarr",
                "movieId": entity_id,
            }
            await self._request(app, "POST", "/api/v3/blocklist", json=payload)
            return "Blocklisted release via /api/v3/blocklist"

        return f"No history record found to blocklist for {release_title}"

    async def _find_grabbed_history_id(
        self, app: AppName, release_title: str, entity_id: int | None, download_id: str | None
    ) -> int | None:
        def match_record(records: list[dict[str, Any]]) -> int | None:
            grabbed = [
                r
                for r in records
                if str(r.get("eventType", "grabbed")).lower()
                in ("grabbed", str(_GRABBED_EVENT_TYPE))
            ]
            if download_id:
                for record in grabbed:
                    if str(record.get("downloadId", "")).lower() == download_id.lower():
                        return int(record["id"])
            for record in grabbed:
                if record.get("sourceTitle") == release_title:
                    return int(record["id"])
            return None

        if entity_id:
            path, params = (
                ("/api/v3/history/movie", {"movieId": entity_id, "eventType": _GRABBED_EVENT_TYPE})
                if app == "radarr"
                else (
                    "/api/v3/history/series",
                    {"seriesId": entity_id, "eventType": _GRABBED_EVENT_TYPE},
                )
            )
            try:
                data = await self._request(app, "GET", path, params=params)
                records = data.get("records", []) if isinstance(data, dict) else (data or [])
                found_id = match_record(records)
                if found_id is not None:
                    return found_id
            except RemediationError as exc:
                logger.warning(
                    "Could not look up grabbed history for entity %s: %s", entity_id, exc
                )

        # Fallback to general history
        try:
            data = await self._request(
                app,
                "GET",
                "/api/v3/history",
                params={"eventType": _GRABBED_EVENT_TYPE, "pageSize": 50},
            )
            records = data.get("records", []) if isinstance(data, dict) else (data or [])
            return match_record(records)
        except RemediationError as exc:
            logger.warning("Could not look up general grabbed history: %s", exc)
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
