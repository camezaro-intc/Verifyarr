"""Tier 0 Pre-Download Inspection Engine: Inspects torrent file trees on Grab events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.config import Settings
from src.models.history import CheckResult, HistoryEntry, HistoryStore, VerificationStatus
from src.models.servarr import AppName
from src.services.notifier import Notifier
from src.services.qbittorrent import QBittorrentClient
from src.services.remediator import Remediator

logger = logging.getLogger(__name__)

# Valid video file extensions considered legitimate media containers
VALID_VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv", ".flv", ".webm"}
)


class PreDownloadInspector:
    """Audits torrent manifests on grab before files are written to disk."""

    def __init__(
        self,
        settings: Settings,
        history: HistoryStore,
        remediator: Remediator,
        notifier: Notifier,
        qbit_client: QBittorrentClient | None = None,
    ) -> None:
        self._settings = settings
        self._history = history
        self._remediator = remediator
        self._notifier = notifier
        self._qbit_client = qbit_client

    def reload_settings(self, settings: Settings) -> None:
        """Update active settings reference and refresh qBittorrent client configuration."""
        self._settings = settings
        if self._qbit_client:
            self._qbit_client.url = settings.qbittorrent.url
            self._qbit_client.username = settings.qbittorrent.username
            self._qbit_client.password = settings.qbittorrent.password.get_secret_value()
            self._qbit_client._authenticated = False

    def get_client(self) -> QBittorrentClient:
        """Return the active qBittorrent client instance, instantiating one if needed."""
        if self._qbit_client is None:
            self._qbit_client = QBittorrentClient(
                url=self._settings.qbittorrent.url,
                username=self._settings.qbittorrent.username,
                password=self._settings.qbittorrent.password.get_secret_value(),
            )
        return self._qbit_client

    async def aclose(self) -> None:
        """Close underlying qBittorrent HTTP connection."""
        if self._qbit_client is not None:
            await self._qbit_client.aclose()

    async def inspect_grab_event(self, event_data: dict[str, Any]) -> bool:
        """Inspect a grabbed torrent.

        Returns True if inspected (either clean or purged), False if skipped.
        """
        qbit_cfg = self._settings.qbittorrent
        if not qbit_cfg.enabled or not qbit_cfg.inspect_on_grab:
            logger.debug("Tier 0 inspection disabled; skipping Grab event")
            return False

        # 1. Extract downloadId (torrent hash) and client info
        download_id = event_data.get("downloadId")
        if not download_id or not isinstance(download_id, str):
            logger.debug("No valid downloadId found in Grab payload; skipping Tier 0 inspection")
            return False
        download_id = download_id.strip()

        # Check download client name
        client_name = (
            str(event_data.get("downloadClient") or event_data.get("downloadClientType") or "")
            .strip()
            .lower()
        )
        if client_name and "qbit" not in client_name:
            logger.info(
                "Download client '%s' is not qBittorrent; skipping Tier 0 inspection", client_name
            )
            return False

        # Extract app, entity, release title
        raw_movie = event_data.get("movie")
        raw_series = event_data.get("series")
        app_name: AppName = "sonarr" if (raw_series or "episodes" in event_data) else "radarr"

        entity: dict[str, Any] = (
            raw_movie
            if isinstance(raw_movie, dict)
            else (raw_series if isinstance(raw_series, dict) else {})
        )
        title = str(entity.get("title") or event_data.get("title") or "Unknown")
        entity_id = entity.get("id")

        raw_rel = event_data.get("release")
        release_dict: dict[str, Any] = raw_rel if isinstance(raw_rel, dict) else {}
        release_title = str(release_dict.get("releaseTitle") or event_data.get("title") or title)
        indexer = release_dict.get("indexer")
        size = release_dict.get("size")

        raw_episodes = event_data.get("episodes")
        episode_ids: list[int] | None = None
        if isinstance(raw_episodes, list):
            parsed_ids = [
                int(ep["id"])
                for ep in raw_episodes
                if isinstance(ep, dict) and ep.get("id") is not None
            ]
            if parsed_ids:
                episode_ids = parsed_ids

        logger.info(
            "Tier 0: Starting pre-download inspection for '%s' (hash: %s, app: %s)",
            release_title,
            download_id,
            app_name,
        )

        qbit = self.get_client()

        # 3. Poll for file tree metadata (up to metadata_timeout_seconds)
        timeout = qbit_cfg.metadata_timeout_seconds
        poll_interval = 3.0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        files: list[dict[str, Any]] = []

        while loop.time() < deadline:
            try:
                files = await qbit.get_torrent_files(download_id)
                if files:
                    break
            except Exception as exc:
                logger.warning("Error fetching torrent files for %s: %s", download_id, exc)
            await asyncio.sleep(poll_interval)

        if not files:
            logger.warning(
                "Tier 0: Torrent %s metadata resolution timed out after %ds; skipping inspection",
                download_id,
                timeout,
            )
            return False

        # 4. Validation logic
        file_names = [f.get("name", "") for f in files if isinstance(f, dict)]
        blocked_exts = [ext.lower().strip() for ext in qbit_cfg.blocked_extensions]

        malicious_files: list[tuple[str, str]] = []
        for name in file_names:
            lname = name.lower()
            for ext in blocked_exts:
                if lname.endswith(ext):
                    malicious_files.append((name, ext))
                    break

        is_malicious = False
        rejection_reason = ""

        if malicious_files:
            is_malicious = True
            exts_detected = sorted({ext for _, ext in malicious_files})
            first_offender = malicious_files[0][0]
            rejection_reason = (
                f"Malicious archive extension ({', '.join(exts_detected)}) "
                f"detected in torrent manifest: {first_offender}"
            )
        else:
            # Check for zero valid video containers
            has_video = any(
                any(name.lower().endswith(ve) for ve in VALID_VIDEO_EXTENSIONS)
                for name in file_names
            )
            if not has_video:
                is_malicious = True
                rejection_reason = (
                    "Zero valid video containers (.mkv, .mp4, .avi, etc.) found in torrent manifest"
                )

        if not is_malicious:
            logger.info(
                "Tier 0: Release '%s' passed pre-download inspection (%d files)",
                release_title,
                len(files),
            )
            return True

        # 5. Remediation Action
        logger.warning("Tier 0: REJECTED_PRE_DOWNLOAD '%s' -> %s", release_title, rejection_reason)
        remediation_actions: list[str] = []

        # A. Delete from qBittorrent
        deleted_qbit = await qbit.delete_torrent(download_id, delete_files=True)
        if deleted_qbit:
            remediation_actions.append(f"Purged torrent {download_id} and data from qBittorrent")
        else:
            remediation_actions.append(f"Failed to delete torrent {download_id} from qBittorrent")

        # B. Call Servarr remediation
        try:
            msg = await self._remediator.remove_from_queue_and_blocklist(
                app_name,
                download_id,
                release_title=release_title,
                entity_id=entity_id or 0,
                indexer=indexer,
                size=size,
                episode_ids=episode_ids,
            )
            remediation_actions.append(str(msg))
        except Exception as exc:
            logger.warning("Tier 0: Servarr blocklist failed for %s: %s", release_title, exc)
            remediation_actions.append(f"Servarr blocklist error: {exc}")

        if entity_id:
            try:
                search_msg = await self._remediator.trigger_search(
                    app_name,
                    entity_id,
                    episode_ids=episode_ids,
                )
                remediation_actions.append(str(search_msg))
            except Exception as exc:
                logger.warning("Tier 0: Servarr re-search failed for %s: %s", release_title, exc)

        # C. Record in History
        check = CheckResult(
            tier=0,
            name="Pre-Download Inspection",
            passed=False,
            detail=rejection_reason,
        )
        entry = HistoryEntry(
            app=app_name,
            title=title,
            release_name=release_title,
            file_path=None,
            status=VerificationStatus.REJECTED_PRE_DOWNLOAD,
            failure_reasons=[rejection_reason],
            checks=[check],
            remediation=remediation_actions,
            download_hash=download_id,
        )
        await self._history.add(entry)

        # D. Send Discord alert
        discord_desc = (
            f"Torrent purged from qBittorrent before downloading.\nReason: {rejection_reason}"
        )
        await self._notifier.send_discord(
            title="🚨 Fake Release Blocked",
            description=discord_desc,
            color=0xE74C3C,
            fields=[
                ("Application", app_name.capitalize()),
                ("Title", title),
                ("Release", release_title),
                ("Hash", download_id),
            ],
        )

        return True
