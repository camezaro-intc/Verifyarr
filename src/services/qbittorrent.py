"""qBittorrent Web API client for Tier 0 pre-download inspection and torrent management."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class QBittorrentError(RuntimeError):
    """Base error for qBittorrent operations."""


class QBittorrentClient:
    """Asynchronous client interacting with qBittorrent Web API v2."""

    def __init__(
        self,
        url: str = "http://localhost:8080",
        username: str = "admin",
        password: str = "",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._authenticated = False
        self._last_error: str = ""

    async def aclose(self) -> None:
        """Close the underlying HTTP client session."""
        await self._client.aclose()

    async def login(self) -> bool:
        """Authenticate with qBittorrent via ``POST /api/v2/auth/login``.

        Supports both qBittorrent v4.x (HTTP 200 'Ok.') and v5.x (HTTP 204 No Content).
        Returns True if login succeeded and session cookie was established.
        """
        login_url = f"{self.url}/api/v2/auth/login"
        headers = {
            "Referer": f"{self.url}/",
            "Origin": self.url,
        }
        try:
            resp = await self._client.post(
                login_url,
                data={"username": self.username, "password": self.password},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            self._last_error = f"Could not connect to qBittorrent: {exc}"
            logger.error("Failed to connect to qBittorrent at %s: %s", login_url, exc)
            raise QBittorrentError(f"Could not connect to qBittorrent: {exc}") from exc

        # qBittorrent v5+ returns 204 No Content; v4 returns 200 with "Ok."
        # Cookies may be named SID, QBT_SID_PORT, etc.
        is_success = (
            resp.status_code == 204
            or (resp.status_code == 200 and resp.text.strip() == "Ok.")
            or (
                resp.status_code in (200, 204)
                and any("sid" in k.lower() or "qbt" in k.lower() for k in resp.cookies.keys())
            )
        )

        if is_success:
            self._authenticated = True
            self._last_error = ""
            logger.info("Authenticated successfully with qBittorrent at %s", self.url)
            return True

        self._authenticated = False
        if resp.status_code == 401:
            self._last_error = "Invalid username or password (HTTP 401)"
        elif resp.status_code == 403:
            self._last_error = "Access forbidden (HTTP 403 - check IP ban or CSRF)"
        elif resp.text.strip() == "Fails.":
            self._last_error = "Invalid username or password"
        else:
            self._last_error = f"HTTP {resp.status_code}: {resp.text.strip() or 'Unauthorized'}"
        logger.warning("qBittorrent login failed (%s): %s", self.url, self._last_error)
        return False

    async def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            ok = await self.login()
            if not ok:
                raise QBittorrentError(self._last_error or "qBittorrent authentication failed")

    async def test_connection(self) -> tuple[bool, str]:
        """Test authentication and query the qBittorrent API version."""
        try:
            # Check if localhost bypass allows access directly without logging in
            try:
                direct = await self._client.get(f"{self.url}/api/v2/app/version")
                if direct.status_code == 200 and direct.text.strip():
                    self._authenticated = True
                    return True, f"Connected to qBittorrent ({direct.text.strip()})"
            except Exception:
                pass

            ok = await self.login()
            if not ok:
                return False, self._last_error or "Invalid credentials or unauthorized"

            ver_resp = await self._client.get(f"{self.url}/api/v2/app/version")
            if ver_resp.status_code == 200 and ver_resp.text.strip():
                return True, f"Connected to qBittorrent ({ver_resp.text.strip()})"

            ver_api = await self._client.get(f"{self.url}/api/v2/app/webapiVersion")
            version_str = ver_api.text.strip() if ver_api.status_code == 200 else "ok"
            return True, f"Connected to qBittorrent (WebAPI v{version_str})"
        except Exception as exc:
            return False, str(exc)

    async def get_torrent_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        """Fetch file list for a torrent via ``GET /api/v2/torrents/files?hash={hash}``.

        Returns an empty list if the torrent has not yet resolved metadata (e.g. magnet)
        or if the torrent is not found.
        """
        await self._ensure_authenticated()
        url = f"{self.url}/api/v2/torrents/files"
        clean_hash = torrent_hash.strip().lower()

        try:
            resp = await self._client.get(url, params={"hash": clean_hash})
            if resp.status_code == 403:
                # Session may have expired; re-authenticate and retry once
                logger.info("qBittorrent session expired; re-authenticating")
                self._authenticated = False
                await self._ensure_authenticated()
                resp = await self._client.get(url, params={"hash": clean_hash})

            if resp.status_code in (404, 400):
                return []
            if resp.status_code != 200:
                logger.warning(
                    "qBittorrent get_torrent_files returned HTTP %d for hash %s",
                    resp.status_code,
                    clean_hash,
                )
                return []

            data = resp.json()
            return data if isinstance(data, list) else []
        except httpx.HTTPError as exc:
            logger.error("Error communicating with qBittorrent get_torrent_files: %s", exc)
            return []

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = True) -> bool:
        """Delete a torrent via ``POST /api/v2/torrents/delete``."""
        await self._ensure_authenticated()
        url = f"{self.url}/api/v2/torrents/delete"
        clean_hash = torrent_hash.strip().lower()

        data = {
            "hashes": clean_hash,
            "deleteFiles": "true" if delete_files else "false",
        }

        try:
            resp = await self._client.post(url, data=data)
            if resp.status_code == 403:
                self._authenticated = False
                await self._ensure_authenticated()
                resp = await self._client.post(url, data=data)

            if resp.status_code == 200:
                logger.info(
                    "Deleted torrent %s from qBittorrent (delete_files=%s)",
                    clean_hash,
                    delete_files,
                )
                return True

            logger.warning(
                "qBittorrent delete_torrent returned HTTP %d for hash %s: %s",
                resp.status_code,
                clean_hash,
                resp.text,
            )
            return False
        except httpx.HTTPError as exc:
            logger.error("Error calling qBittorrent delete_torrent: %s", exc)
            return False
