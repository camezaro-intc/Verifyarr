"""Outbound notifications: Jellyfin library refresh and Discord webhook messages."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from src.config import Settings
from src.models.history import HistoryEntry, VerificationStatus

logger = logging.getLogger(__name__)

# Season packs finish many files in quick succession; coalesce them into one Jellyfin scan.
JELLYFIN_REFRESH_DEBOUNCE_SECONDS = 10.0

_COLOR_RED = 0xE74C3C
_COLOR_ORANGE = 0xF39C12
_COLOR_GREY = 0x95A5A6


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Notifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._pending_refresh: asyncio.Task[None] | None = None

    def reload_settings(self, settings: Settings) -> None:
        """Update the active settings reference in memory."""
        self._settings = settings

    async def aclose(self) -> None:
        if self._pending_refresh and not self._pending_refresh.done():
            self._pending_refresh.cancel()
        await self._client.aclose()

    # -- Jellyfin ------------------------------------------------------------------

    async def refresh_jellyfin(self, *, immediate: bool = False) -> bool:
        """Ask Jellyfin to scan its libraries.

        ``immediate=False`` debounces/coalesces requests and returns ``True`` once scheduled;
        ``immediate=True`` calls Jellyfin now and returns whether the call succeeded.
        """
        config = self._settings.jellyfin
        if not config.enabled:
            logger.debug("Jellyfin integration disabled; skipping refresh")
            return False
        if immediate:
            return await self._post_jellyfin_refresh()
        if self._pending_refresh is None or self._pending_refresh.done():
            self._pending_refresh = asyncio.create_task(self._debounced_refresh())
        return True

    async def _debounced_refresh(self) -> None:
        await asyncio.sleep(JELLYFIN_REFRESH_DEBOUNCE_SECONDS)
        await self._post_jellyfin_refresh()

    async def _post_jellyfin_refresh(self) -> bool:
        config = self._settings.jellyfin
        try:
            response = await self._client.post(
                f"{config.url}/Library/Refresh",
                headers={"X-Emby-Token": config.api_key.get_secret_value()},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("Jellyfin library refresh failed: %r", exc)
            return False
        logger.info("Jellyfin library refresh requested")
        return True

    # -- Discord -------------------------------------------------------------------

    async def send_discord(
        self,
        title: str,
        description: str,
        *,
        color: int = _COLOR_GREY,
        fields: list[tuple[str, str]] | None = None,
    ) -> bool:
        url = self._settings.notifications.discord_webhook_url.strip()
        if not url:
            return False
        embed: dict[str, Any] = {
            "title": _clip(title, 256),
            "description": _clip(description, 4000),
            "color": color,
            "fields": [
                {"name": _clip(name, 256), "value": _clip(value or "-", 1024), "inline": False}
                for name, value in (fields or [])
            ],
        }
        body = {"username": "Verifyarr", "embeds": [embed]}
        for attempt in (1, 2):
            try:
                response = await self._client.post(url, json=body)
                if response.status_code == 429 and attempt == 1:
                    retry_after = float(response.json().get("retry_after", 1))
                    await asyncio.sleep(min(retry_after, 10))
                    continue
                response.raise_for_status()
                return True
            except (httpx.HTTPError, ValueError) as exc:
                logger.error("Discord notification failed: %r", exc)
                return False
        return False

    async def notify_failure(self, entry: HistoryEntry) -> bool:
        """Post a failure summary (a rejection, or a would-be rejection in dry-run mode)."""
        dry = entry.status == VerificationStatus.DRY_RUN
        title = f"{'[DRY RUN] Would reject' if dry else 'Rejected'}: {entry.title}"
        expected = (
            f"{entry.expected_runtime_min:.0f} min" if entry.expected_runtime_min else "unknown"
        )
        probed = f"{entry.probed_runtime_min:.1f} min" if entry.probed_runtime_min else "unknown"
        fields = [
            ("Release", entry.release_name),
            ("Runtime (probed / expected)", f"{probed} / {expected}"),
            ("Reasons", "\n".join(f"• {r}" for r in entry.failure_reasons) or "-"),
        ]
        if entry.remediation:
            fields.append(("Actions", "\n".join(f"• {a}" for a in entry.remediation)))
        return await self.send_discord(
            title,
            f"{entry.app.capitalize()} import failed Verifyarr verification.",
            color=_COLOR_ORANGE if dry else _COLOR_RED,
            fields=fields,
        )

    async def notify_error(self, entry: HistoryEntry) -> bool:
        """Report that a file could not be verified (no remediation is performed)."""
        return await self.send_discord(
            f"Verification error: {entry.title}",
            "\n".join(entry.failure_reasons) or "Unknown error",
            color=_COLOR_GREY,
            fields=[("Release", entry.release_name)],
        )
