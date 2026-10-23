"""Correlated log aggregator service for Verifyarr, qBittorrent, Servarr, and Jellyfin."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from src.config import Settings
from src.models.history import HistoryEntry, HistoryStore
from src.routers.system import resolve_log_path

logger = logging.getLogger(__name__)

# Noise tokens commonly found in scene release names to omit from keyword matching
COMMON_NOISE_TOKENS = {
    "1080p", "720p", "2160p", "480p", "4k", "uhd", "hdr", "web", "dl",
    "web-dl", "webrip", "bluray", "brrip", "remux", "h264", "x264", "h265",
    "x265", "hevc", "aac", "ac3", "dts", "ddp", "ddp5", "atmos", "truehd",
    "repack", "proper", "flux", "ntb", "ettv", "yify", "rarbg", "amzn",
    "nf", "hmax", "dsnp", "atvp",
}

DEFAULT_SONARR_LOG = Path(r"C:\ProgramData\Sonarr\logs\sonarr.txt")
DEFAULT_RADARR_LOG = Path(r"C:\ProgramData\Radarr\logs\radarr.txt")
DEFAULT_JELLYFIN_LOG_DIR = Path(r"C:\ProgramData\Jellyfin\Server\log")

# Regex for Verifyarr log lines: 2026-10-23T14:00:00-06:00 INFO src.main: Message
VERIFYARR_LOG_REGEX = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T[\d:\.\-+]+)\s+([A-Z]+)\s+([\w\.\-]+):\s+(.*)$"
)

# Regex for Sonarr/Radarr log lines: 2026-10-23 14:15:20.1|Info|DownloadDecisionMaker|Message
SERVARR_LOG_REGEX = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"\s*\|([A-Za-z]+)\|\s*(?:[\w\.\-]+)\|\s*(.*)$"
)
SERVARR_BRACKET_REGEX = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"\s*\[([A-Za-z]+)\]\s*(?:[\w\.\-]+:)?\s*(.*)$"
)

# Regex for Jellyfin log lines: [2026-10-23 14:20:15.123 -05:00] [INF] [1] Jellyfin...: Message
JELLYFIN_LOG_REGEX = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:\s*[Zz]|\s*[\+\-]\d{2}:?\d{2})?)\]\s+\[([A-Za-z]{3,4})\]\s+(?:\[\d+\]\s+)?(.*)$"
)


def extract_keywords(release_name: str, title: str) -> list[str]:
    """Extract distinct meaningful keywords from release name and title."""
    tokens = re.findall(r"[A-Za-z0-9]+", release_name or "")
    keywords: list[str] = []
    for token in tokens:
        tl = token.lower()
        if len(tl) >= 3 and tl not in COMMON_NOISE_TOKENS:
            keywords.append(tl)

    if title:
        for token in re.findall(r"[A-Za-z0-9]+", title):
            tl = token.lower()
            if len(tl) >= 3 and tl not in COMMON_NOISE_TOKENS:
                keywords.append(tl)

    return list(dict.fromkeys(keywords))


def normalize_level(level_raw: str) -> str:
    """Normalize service log levels into INFO | WARN | ERROR."""
    lvl = (level_raw or "").strip().upper()
    if lvl in ("ERROR", "ERR", "CRITICAL", "FATAL", "FTL"):
        return "ERROR"
    if lvl in ("WARNING", "WARN", "WRN"):
        return "WARN"
    return "INFO"


def parse_timestamp(ts_str: str) -> datetime | None:
    """Parse timestamp strings from ISO or common service formats into a datetime."""
    cleaned = ts_str.strip()
    # Normalize space before timezone offset (e.g. '2026-10-23 14:35:00.000 +00:00')
    cleaned_tz = re.sub(r"\s+([\+\-]\d{2}:?\d{2})$", r"\1", cleaned)

    try:
        return datetime.fromisoformat(cleaned_tz)
    except ValueError:
        pass

    # Try replacing space with 'T' (e.g. '2026-10-23 14:15:20.123')
    try:
        normalized = cleaned_tz.replace(" ", "T")
        return datetime.fromisoformat(normalized)
    except ValueError:
        pass

    # Fallback with strptime for common formats
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    return None


def is_in_window(dt: datetime, window_start: datetime, window_end: datetime) -> bool:
    """Determine whether datetime falls in [window_start, window_end] with tz reconciliation."""
    test_dt = dt
    start = window_start
    end = window_end

    # If test_dt is naive but window is aware, assume local or window's tz
    if test_dt.tzinfo is None and start.tzinfo is not None:
        test_dt = test_dt.replace(tzinfo=start.tzinfo)
    elif test_dt.tzinfo is not None and start.tzinfo is None:
        start = start.replace(tzinfo=test_dt.tzinfo)
        end = end.replace(tzinfo=test_dt.tzinfo)

    return start <= test_dt <= end


class LogAggregator:
    """Aggregates and correlates logs across Verifyarr, qBittorrent, Servarr, and Jellyfin."""

    def __init__(
        self,
        settings: Settings | None = None,
        history_store: HistoryStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._history = history_store
        self._http_client = http_client

    async def get_correlated_logs(self, history_id: int) -> dict[str, Any] | None:
        """Retrieve correlated logs for a given verification history item ID."""
        if self._history is None:
            raise RuntimeError("HistoryStore is required to lookup history item")
        record = await self._history.get(history_id)
        if record is None:
            return None
        return await self.aggregate_for_record(record)

    async def aggregate(self, history_id: int) -> dict[str, Any] | None:
        """Alias for get_correlated_logs."""
        return await self.get_correlated_logs(history_id)

    async def aggregate_for_record(self, record: HistoryEntry) -> dict[str, Any]:
        """Aggregate log lines across all available services for a retrieved history record."""
        event_dt = record.timestamp
        # Calculate time window: [event_time - 45 min] to [event_time + 45 min]
        window_start = event_dt - timedelta(minutes=45)
        window_end = event_dt + timedelta(minutes=45)

        title = record.title or ""
        release_name = record.release_name or ""
        hash_val = record.download_hash
        source_app = record.source_app or record.app or ""
        file_path = record.file_path

        # Gather events concurrently from all 4 data collectors
        results = await asyncio.gather(
            self.collect_qbittorrent(window_start, window_end, hash_val, release_name, title),
            self.collect_verifyarr(window_start, window_end, hash_val, release_name, title),
            self.collect_servarr(
                source_app, window_start, window_end, hash_val, release_name, title
            ),
            self.collect_jellyfin(window_start, window_end, file_path, release_name, title),
            return_exceptions=True,
        )

        merged_events: list[dict[str, Any]] = []
        collector_names = ["qBittorrent", "Verifyarr", "Servarr", "Jellyfin"]

        for idx, res in enumerate(results):
            if isinstance(res, Exception):
                logger.warning(
                    "Error during %s log collection: %s",
                    collector_names[idx],
                    res,
                )
            elif isinstance(res, list):
                merged_events.extend(res)

        # Sort all events chronologically ascending by timestamp
        def sort_key(item: dict[str, Any]) -> tuple[datetime, str]:
            parsed = parse_timestamp(item.get("timestamp", ""))
            if parsed is None:
                parsed = datetime.min.replace(tzinfo=UTC)
            elif parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return (parsed, item.get("app", ""))

        merged_events.sort(key=sort_key)

        # Remove internal sort keys if present and sanitize output
        cleaned_events = [
            {
                "timestamp": e.get("timestamp", ""),
                "app": e.get("app", ""),
                "level": e.get("level", "INFO"),
                "message": e.get("message", ""),
            }
            for e in merged_events
        ]

        return {
            "title": record.title,
            "release_name": record.release_name,
            "events": cleaned_events,
        }

    # --------------------------------------------------------------------------
    # 1. qBittorrent Collector (Web API)
    # --------------------------------------------------------------------------
    async def collect_qbittorrent(
        self,
        window_start: datetime,
        window_end: datetime,
        hash_val: str | None,
        release_name: str,
        title: str,
    ) -> list[dict[str, Any]]:
        """Collect logs from qBittorrent Web API falling within the time window."""
        events: list[dict[str, Any]] = []
        base_url = "http://localhost:8080"
        if self._settings and getattr(self._settings, "qbittorrent", None):
            qbit_cfg = self._settings.qbittorrent
            if getattr(qbit_cfg, "url", None):
                base_url = qbit_cfg.url.rstrip("/")

        endpoint = f"{base_url}/api/v2/log/main"
        params = {
            "normal": "true",
            "info": "true",
            "warning": "true",
            "critical": "true",
        }

        keywords = extract_keywords(release_name, title)
        hash_clean = hash_val.lower().strip() if hash_val else None

        client_to_close: httpx.AsyncClient | None = None
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=5.0)
            client_to_close = client

        try:
            resp = await client.get(endpoint, params=params)
            # Handle potential authentication requirement
            if (
                resp.status_code in (401, 403)
                and self._settings
                and getattr(self._settings, "qbittorrent", None)
            ):
                qbit_cfg = self._settings.qbittorrent
                username = getattr(qbit_cfg, "username", "admin")
                password = getattr(qbit_cfg, "password", "")
                login_resp = await client.post(
                    f"{base_url}/api/v2/auth/login",
                    data={"username": username, "password": password},
                    headers={"Referer": f"{base_url}/"},
                )
                if login_resp.status_code in (200, 204):
                    resp = await client.get(endpoint, params=params)

            if resp.status_code != 200:
                logger.warning(
                    "qBittorrent log API returned status %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return []

            data = resp.json()
            if not isinstance(data, list):
                return []

            for item in data:
                if not isinstance(item, dict):
                    continue

                ts_val = item.get("timestamp")
                if ts_val is None:
                    continue

                try:
                    ts_float = float(ts_val)
                    if ts_float > 1e11:  # Milliseconds epoch
                        dt = datetime.fromtimestamp(ts_float / 1000.0, tz=UTC)
                    else:  # Seconds epoch
                        dt = datetime.fromtimestamp(ts_float, tz=UTC)
                except (ValueError, OSError):
                    continue

                if not is_in_window(dt, window_start, window_end):
                    continue

                msg = str(item.get("message", ""))
                msg_lower = msg.lower()

                # Filter: hash in message OR release keywords in message
                hash_match = hash_clean and (hash_clean in msg_lower)
                kw_match = any(kw in msg_lower for kw in keywords)

                if not (hash_match or kw_match):
                    continue

                # qBittorrent log type bitmask: 1=Normal, 2=Info, 4=Warning, 8=Critical
                qtype = item.get("type", 1)
                if qtype == 4:
                    level = "WARN"
                elif qtype == 8:
                    level = "ERROR"
                elif "warning" in msg_lower:
                    level = "WARN"
                elif "error" in msg_lower or "critical" in msg_lower:
                    level = "ERROR"
                else:
                    level = "INFO"

                events.append({
                    "timestamp": dt.astimezone(UTC).isoformat(),
                    "app": "qBittorrent",
                    "level": level,
                    "message": msg,
                })
        except Exception as exc:
            logger.warning("Failed to collect logs from qBittorrent: %s", exc)
        finally:
            if client_to_close:
                await client_to_close.aclose()

        return events

    # --------------------------------------------------------------------------
    # 2. Verifyarr Collector (Local File)
    # --------------------------------------------------------------------------
    async def collect_verifyarr(
        self,
        window_start: datetime,
        window_end: datetime,
        hash_val: str | None,
        release_name: str,
        title: str,
    ) -> list[dict[str, Any]]:
        """Collect logs from verifyarr.log matching the record in the time window."""
        log_path = resolve_log_path()
        return await asyncio.to_thread(
            self._read_verifyarr_file,
            log_path,
            window_start,
            window_end,
            hash_val,
            release_name,
            title,
        )

    def _read_verifyarr_file(
        self,
        log_path: Path,
        window_start: datetime,
        window_end: datetime,
        hash_val: str | None,
        release_name: str,
        title: str,
    ) -> list[dict[str, Any]]:
        if not log_path.exists():
            logger.warning("Verifyarr log file not found at %s", log_path)
            return []

        events: list[dict[str, Any]] = []
        rel_lower = (release_name or "").lower().strip()
        title_lower = (title or "").lower().strip()
        hash_lower = (hash_val or "").lower().strip() if hash_val else None

        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("Could not read Verifyarr log file at %s: %s", log_path, exc)
            return []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            match = VERIFYARR_LOG_REGEX.match(stripped)
            if not match:
                continue

            ts_str, lvl_str, _, message = match.groups()
            dt = parse_timestamp(ts_str)
            if dt is None or not is_in_window(dt, window_start, window_end):
                continue

            line_lower = stripped.lower()
            rel_match = rel_lower and (rel_lower in line_lower)
            title_match = title_lower and (title_lower in line_lower)
            hash_match = hash_lower and (hash_lower in line_lower)

            if not (rel_match or title_match or hash_match):
                continue

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=window_start.tzinfo or UTC)
            dt_utc = dt.astimezone(UTC)

            events.append({
                "timestamp": dt_utc.isoformat(),
                "app": "Verifyarr",
                "level": normalize_level(lvl_str),
                "message": message,
            })

        return events

    # --------------------------------------------------------------------------
    # 3. Sonarr / Radarr Collector (Local File)
    # --------------------------------------------------------------------------
    async def collect_servarr(
        self,
        source_app: str,
        window_start: datetime,
        window_end: datetime,
        hash_val: str | None,
        release_name: str,
        title: str,
    ) -> list[dict[str, Any]]:
        """Collect logs from Sonarr or Radarr matching the record in the time window."""
        app_lower = (source_app or "").lower().strip()
        if "sonarr" in app_lower:
            target_app = "Sonarr"
            log_path = Path(os.getenv("SONARR_LOG_PATH") or DEFAULT_SONARR_LOG)
        else:
            target_app = "Radarr"
            log_path = Path(os.getenv("RADARR_LOG_PATH") or DEFAULT_RADARR_LOG)

        return await asyncio.to_thread(
            self._read_servarr_file,
            target_app,
            log_path,
            window_start,
            window_end,
            hash_val,
            title,
        )

    def _read_servarr_file(
        self,
        app_name: str,
        log_path: Path,
        window_start: datetime,
        window_end: datetime,
        hash_val: str | None,
        title: str,
    ) -> list[dict[str, Any]]:
        if not log_path.exists():
            logger.warning("%s log file not found at %s", app_name, log_path)
            return []

        events: list[dict[str, Any]] = []
        title_lower = (title or "").lower().strip()
        hash_lower = (hash_val or "").lower().strip() if hash_val else None

        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("Could not read %s log file at %s: %s", app_name, log_path, exc)
            return []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            dt: datetime | None = None
            lvl_str = "INFO"
            message = stripped

            m_pipe = SERVARR_LOG_REGEX.match(stripped)
            m_bracket = SERVARR_BRACKET_REGEX.match(stripped)

            if m_pipe:
                ts_str, lvl_str, message = m_pipe.groups()
                dt = parse_timestamp(ts_str)
            elif m_bracket:
                ts_str, lvl_str, message = m_bracket.groups()
                dt = parse_timestamp(ts_str)
            else:
                # Try generic leading timestamp extraction
                m_gen = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)", stripped)
                if m_gen:
                    dt = parse_timestamp(m_gen.group(1))

            if dt is None or not is_in_window(dt, window_start, window_end):
                continue

            line_lower = stripped.lower()
            title_match = title_lower and (title_lower in line_lower)
            hash_match = hash_lower and (hash_lower in line_lower)

            if not (title_match or hash_match):
                continue

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=window_start.tzinfo or UTC)
            dt_utc = dt.astimezone(UTC)

            events.append({
                "timestamp": dt_utc.isoformat(),
                "app": app_name,
                "level": normalize_level(lvl_str),
                "message": message,
            })

        return events

    # --------------------------------------------------------------------------
    # 4. Jellyfin Collector (Local Directory)
    # --------------------------------------------------------------------------
    async def collect_jellyfin(
        self,
        window_start: datetime,
        window_end: datetime,
        file_path: str | None,
        release_name: str,
        title: str,
    ) -> list[dict[str, Any]]:
        """Collect logs from the most recent Jellyfin log file in the log directory."""
        log_dir = Path(os.getenv("JELLYFIN_LOG_DIR") or DEFAULT_JELLYFIN_LOG_DIR)
        return await asyncio.to_thread(
            self._read_jellyfin_directory,
            log_dir,
            window_start,
            window_end,
            file_path,
            release_name,
            title,
        )

    def _read_jellyfin_directory(
        self,
        log_dir: Path,
        window_start: datetime,
        window_end: datetime,
        file_path: str | None,
        release_name: str,
        title: str,
    ) -> list[dict[str, Any]]:
        if not log_dir.exists() or not log_dir.is_dir():
            logger.warning("Jellyfin log directory not found at %s", log_dir)
            return []

        events: list[dict[str, Any]] = []

        try:
            candidates = list(log_dir.glob("log_*.log"))
            if not candidates:
                candidates = list(log_dir.glob("*.log"))

            if not candidates:
                logger.warning("No log files found in Jellyfin log directory %s", log_dir)
                return []

            latest_file = max(candidates, key=lambda p: p.stat().st_mtime)
            with open(latest_file, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("Could not read Jellyfin logs in %s: %s", log_dir, exc)
            return []

        media_name = Path(file_path).name.lower() if file_path else None
        title_lower = (title or "").lower().strip()
        rel_lower = (release_name or "").lower().strip()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            match = JELLYFIN_LOG_REGEX.match(stripped)
            if not match:
                continue

            ts_str, lvl_str, message = match.groups()
            dt = parse_timestamp(ts_str)
            if dt is None or not is_in_window(dt, window_start, window_end):
                continue

            line_lower = stripped.lower()
            media_match = media_name and (media_name in line_lower)
            title_match = title_lower and (title_lower in line_lower)
            rel_match = rel_lower and (rel_lower in line_lower)

            if not (media_match or title_match or rel_match):
                continue

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=window_start.tzinfo or UTC)
            dt_utc = dt.astimezone(UTC)

            events.append({
                "timestamp": dt_utc.isoformat(),
                "app": "Jellyfin",
                "level": normalize_level(lvl_str),
                "message": message,
            })

        return events
