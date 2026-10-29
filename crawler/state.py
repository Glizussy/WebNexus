"""Persistent crawl state storage interfaces and SQLite/In-Memory implementations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import sqlite3
from typing import Protocol

from crawler.models import FetchStatus, URLRecord

logger = logging.getLogger(__name__)


def compute_content_hash(data: bytes | str | None) -> str | None:
    """Compute SHA-256 hex digest of page content."""
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


class CrawlStateStore(Protocol):
    """Protocol for crawl state storage to survive restarts."""

    async def get_record(self, canonical_url: str) -> URLRecord | None:
        ...

    async def save_record(self, record: URLRecord) -> None:
        ...

    async def get_due_records(self, before: datetime, limit: int = 100) -> list[URLRecord]:
        ...

    async def list_all_records(self) -> list[URLRecord]:
        ...

    async def close(self) -> None:
        ...


class InMemoryCrawlStateStore:
    """In-memory state store for isolated, fast unit tests."""

    def __init__(self) -> None:
        self._records: dict[str, URLRecord] = {}
        self._lock = asyncio.Lock()

    async def get_record(self, canonical_url: str) -> URLRecord | None:
        async with self._lock:
            return self._records.get(canonical_url)

    async def save_record(self, record: URLRecord) -> None:
        async with self._lock:
            self._records[record.url] = record

    async def get_due_records(self, before: datetime, limit: int = 100) -> list[URLRecord]:
        async with self._lock:
            due: list[URLRecord] = []
            for rec in self._records.values():
                if rec.next_fetch_at and rec.next_fetch_at <= before:
                    due.append(rec)
                    if len(due) >= limit:
                        break
            return due

    async def list_all_records(self) -> list[URLRecord]:
        async with self._lock:
            return list(self._records.values())

    async def close(self) -> None:
        pass


class SQLiteCrawlStateStore:
    """Production persistent crawl state store using SQLite.

    Ensures crawl history, HTTP conditional validators (ETag/Last-Modified),
    and recrawl schedules survive application restarts.
    """

    def __init__(self, db_path: str = "crawl_state.db") -> None:
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS crawl_records (
                    canonical_url TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_crawled_at TEXT,
                    status TEXT NOT NULL,
                    status_code INTEGER,
                    etag TEXT,
                    last_modified TEXT,
                    content_hash TEXT,
                    next_fetch_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    crawl_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_next_fetch ON crawl_records(next_fetch_at)"
            )
        self._conn = conn

    def _parse_iso(self, val: str | None) -> datetime | None:
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return None

    async def get_record(self, canonical_url: str) -> URLRecord | None:
        async with self._lock:
            assert self._conn is not None
            cursor = self._conn.execute(
                "SELECT * FROM crawl_records WHERE canonical_url = ?",
                (canonical_url,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    async def save_record(self, record: URLRecord) -> None:
        async with self._lock:
            assert self._conn is not None
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO crawl_records (
                        canonical_url, first_seen_at, last_crawled_at, status,
                        status_code, etag, last_modified, content_hash,
                        next_fetch_at, consecutive_failures, crawl_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(canonical_url) DO UPDATE SET
                        last_crawled_at = excluded.last_crawled_at,
                        status = excluded.status,
                        status_code = excluded.status_code,
                        etag = excluded.etag,
                        last_modified = excluded.last_modified,
                        content_hash = excluded.content_hash,
                        next_fetch_at = excluded.next_fetch_at,
                        consecutive_failures = excluded.consecutive_failures,
                        crawl_count = excluded.crawl_count
                    """,
                    (
                        record.url,
                        record.first_seen_at.isoformat(),
                        record.last_crawled_at.isoformat() if record.last_crawled_at else None,
                        record.status.value,
                        record.status_code,
                        record.etag,
                        record.last_modified,
                        record.content_hash,
                        record.next_fetch_at.isoformat() if record.next_fetch_at else None,
                        record.consecutive_failures,
                        record.crawl_count,
                    ),
                )

    async def get_due_records(self, before: datetime, limit: int = 100) -> list[URLRecord]:
        async with self._lock:
            assert self._conn is not None
            cursor = self._conn.execute(
                """
                SELECT * FROM crawl_records
                WHERE next_fetch_at IS NOT NULL AND next_fetch_at <= ?
                ORDER BY next_fetch_at ASC
                LIMIT ?
                """,
                (before.isoformat(), limit),
            )
            rows = cursor.fetchall()
            return [self._row_to_record(r) for r in rows]

    async def list_all_records(self) -> list[URLRecord]:
        async with self._lock:
            assert self._conn is not None
            cursor = self._conn.execute("SELECT * FROM crawl_records")
            return [self._row_to_record(r) for r in cursor.fetchall()]

    def _row_to_record(self, row: sqlite3.Row) -> URLRecord:
        return URLRecord(
            url=row["canonical_url"],
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_crawled_at=self._parse_iso(row["last_crawled_at"]),
            status=FetchStatus(row["status"]),
            status_code=row["status_code"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            content_hash=row["content_hash"],
            next_fetch_at=self._parse_iso(row["next_fetch_at"]),
            consecutive_failures=row["consecutive_failures"],
            crawl_count=row["crawl_count"],
        )

    async def close(self) -> None:
        async with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
