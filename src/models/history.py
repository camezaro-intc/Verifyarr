"""Verification history: schema models and a SQLite-backed store.

SQLite calls are synchronous, so every public coroutine delegates to a worker thread via
``asyncio.to_thread`` and serialises access with a lock. That keeps the event loop free
while still using only the standard library. Use ``database_path: ":memory:"`` for a
non-persistent, in-memory history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class VerificationStatus(StrEnum):
    PASSED = "PASSED"
    REJECTED = "REJECTED"  # failed verification and the release was remediated
    DRY_RUN = "DRY_RUN"  # failed verification, but dry-run mode prevented remediation
    ERROR = "ERROR"  # could not be verified (missing file, ffprobe failure, ...); never remediated
    REJECTED_PRE_DOWNLOAD = "REJECTED_PRE_DOWNLOAD"  # fake release purged prior to download


class CheckResult(BaseModel):
    tier: int
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False


class HistoryEntry(BaseModel):
    id: int | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    app: str
    title: str
    release_name: str
    file_path: str | None = None
    file_id: int | None = None
    expected_runtime_min: float | None = None
    probed_runtime_min: float | None = None
    status: VerificationStatus
    failure_reasons: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    remediation: list[str] = Field(default_factory=list)
    whitelisted: bool = False

    @property
    def can_force_approve(self) -> bool:
        return (
            self.status in (VerificationStatus.REJECTED, VerificationStatus.DRY_RUN)
            and not self.whitelisted
        )

    @property
    def timestamp_display(self) -> str:
        return self.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def whitelist_key(release_name: str) -> str:
    return release_name.strip().casefold()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    app TEXT NOT NULL,
    title TEXT NOT NULL,
    release_name TEXT NOT NULL,
    file_path TEXT,
    file_id INTEGER,
    expected_runtime_min REAL,
    probed_runtime_min REAL,
    status TEXT NOT NULL,
    failure_reasons TEXT NOT NULL DEFAULT '[]',
    checks TEXT NOT NULL DEFAULT '[]',
    remediation TEXT NOT NULL DEFAULT '[]',
    whitelisted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS whitelist (
    release_key TEXT PRIMARY KEY,
    added_at TEXT NOT NULL
);
"""


class HistoryStore:
    def __init__(self, database_path: str) -> None:
        self._database_path = database_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        db_path = self._database_path
        if db_path != ":memory:":
            db_path = os.path.abspath(os.path.expanduser(db_path))
            db_dir = os.path.dirname(db_path)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            self._database_path = db_path
        conn = sqlite3.connect(self._database_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        with self._lock:
            if self._database_path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        logger.info("History store ready at %s (WAL enabled)", self._database_path)

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("HistoryStore is not open")
        return self._conn

    # -- history -------------------------------------------------------------------

    async def add(self, entry: HistoryEntry) -> HistoryEntry:
        return await asyncio.to_thread(self._add, entry)

    def _add(self, entry: HistoryEntry) -> HistoryEntry:
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                """INSERT INTO history (timestamp, app, title, release_name, file_path, file_id,
                       expected_runtime_min, probed_runtime_min, status, failure_reasons,
                       checks, remediation, whitelisted)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.timestamp.isoformat(),
                    entry.app,
                    entry.title,
                    entry.release_name,
                    entry.file_path,
                    entry.file_id,
                    entry.expected_runtime_min,
                    entry.probed_runtime_min,
                    entry.status.value,
                    json.dumps(entry.failure_reasons),
                    json.dumps([c.model_dump() for c in entry.checks]),
                    json.dumps(entry.remediation),
                    int(entry.whitelisted),
                ),
            )
            conn.commit()
            saved = entry.model_copy(update={"id": cursor.lastrowid})
        logger.info(
            "SQLite committed history id=%s status=%s title='%s' release='%s'",
            saved.id,
            saved.status.value,
            saved.title,
            saved.release_name,
        )
        return saved

    async def recent(self, limit: int = 50) -> list[HistoryEntry]:
        return await asyncio.to_thread(self._recent, limit)

    def _recent(self, limit: int) -> list[HistoryEntry]:
        with self._lock:
            rows = (
                self._connection()
                .execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))
                .fetchall()
            )
        return [self._row_to_entry(r) for r in rows]

    async def get(self, item_id: int) -> HistoryEntry | None:
        return await asyncio.to_thread(self._get, item_id)

    def _get(self, item_id: int) -> HistoryEntry | None:
        with self._lock:
            row = (
                self._connection()
                .execute("SELECT * FROM history WHERE id = ?", (item_id,))
                .fetchone()
            )
        return self._row_to_entry(row) if row else None

    # -- whitelist -----------------------------------------------------------------

    async def whitelist(self, item_id: int) -> HistoryEntry | None:
        """Mark an entry as force-approved and remember its release for future imports."""
        return await asyncio.to_thread(self._whitelist, item_id)

    def _whitelist(self, item_id: int) -> HistoryEntry | None:
        with self._lock:
            conn = self._connection()
            row = conn.execute("SELECT * FROM history WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE history SET whitelisted = 1 WHERE id = ?", (item_id,))
            conn.execute(
                "INSERT OR IGNORE INTO whitelist (release_key, added_at) VALUES (?, ?)",
                (whitelist_key(row["release_name"]), datetime.now(UTC).isoformat()),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM history WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_entry(row)

    async def is_whitelisted(self, release_name: str) -> bool:
        return await asyncio.to_thread(self._is_whitelisted, release_name)

    def _is_whitelisted(self, release_name: str) -> bool:
        with self._lock:
            row = (
                self._connection()
                .execute(
                    "SELECT 1 FROM whitelist WHERE release_key = ?", (whitelist_key(release_name),)
                )
                .fetchone()
            )
        return row is not None

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
        data: dict[str, Any] = dict(row)
        data["failure_reasons"] = json.loads(data["failure_reasons"])
        data["checks"] = json.loads(data["checks"])
        data["remediation"] = json.loads(data["remediation"])
        data["whitelisted"] = bool(data["whitelisted"])
        return HistoryEntry.model_validate(data)
