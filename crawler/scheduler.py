"""Scheduler managing incremental fetching, conditional request validators, and persistent recrawl schedules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from crawler.config import CrawlerConfig
from crawler.models import CrawlRequest, FetchResult, FetchStatus, URLRecord
from crawler.state import CrawlStateStore, compute_content_hash
from crawler.url_manager import URLManager, canonicalize_url

logger = logging.getLogger(__name__)


class CrawlScheduler:
    """Coordinates incremental crawling, conditional HTTP headers, and state persistence across restarts."""

    def __init__(
        self,
        config: CrawlerConfig,
        state_store: CrawlStateStore,
        url_manager: URLManager,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.url_manager = url_manager

    async def initialize(self) -> None:
        """Restore seen state and load any pending due recrawls from persistent storage."""
        records = await self.state_store.list_all_records()
        for rec in records:
            self.url_manager.mark_seen(rec.url)
        logger.info(f"Initialized scheduler with {len(records)} existing persistent crawl records")

    async def schedule(self, request: CrawlRequest, force: bool = False) -> bool:
        """Schedule a request, attaching stored ETag and Last-Modified validators if previously crawled."""
        try:
            canonical = canonicalize_url(request.url)
        except Exception:
            return False

        existing_record = await self.state_store.get_record(canonical)
        now = datetime.now(timezone.utc)

        if existing_record is not None:
            if not force:
                # Check if this URL is already crawled and not yet due for incremental recrawl
                if existing_record.next_fetch_at and existing_record.next_fetch_at > now:
                    logger.debug(
                        f"Skipping premature recrawl for {canonical}. Next fetch due at {existing_record.next_fetch_at}"
                    )
                    return False

            # Attach previous conditional validators
            if not request.etag and existing_record.etag:
                request.etag = existing_record.etag
            if not request.last_modified and existing_record.last_modified:
                request.last_modified = existing_record.last_modified

        request.url = canonical
        return self.url_manager.add_request(request, force=force)

    async def record_outcome(
        self,
        result: FetchResult,
        recrawl_interval_sec: float | None = None,
    ) -> None:
        """Record the outcome of a fetch and schedule the next recrawl time."""
        now = datetime.now(timezone.utc)
        interval = recrawl_interval_sec or self.config.default_recrawl_interval_sec

        try:
            canonical = canonicalize_url(result.final_url or result.requested_url)
        except Exception:
            canonical = result.final_url or result.requested_url

        existing = await self.state_store.get_record(canonical)
        first_seen = existing.first_seen_at if existing else now
        prev_crawl_count = existing.crawl_count if existing else 0

        if result.not_modified:
            # HTTP 304: Content is unchanged, preserve previous validators and hash
            etag = result.etag or (existing.etag if existing else None)
            last_mod = result.last_modified or (existing.last_modified if existing else None)
            content_hash = existing.content_hash if existing else None
            next_fetch = now + timedelta(seconds=interval)
            consecutive_failures = 0
            status = FetchStatus.NOT_MODIFIED

        elif result.is_success:
            # HTTP 200: Successfully fetched new/modified content
            etag = result.etag or (existing.etag if existing else None)
            last_mod = result.last_modified or (existing.last_modified if existing else None)
            content_hash = compute_content_hash(result.body)
            next_fetch = now + timedelta(seconds=interval)
            consecutive_failures = 0
            status = FetchStatus.SUCCESS

        else:
            # Fetch failed: apply exponential backoff to next retry
            failures = (existing.consecutive_failures if existing else 0) + 1
            consecutive_failures = failures
            # Backoff between 5 minutes and 24 hours
            fail_delay = min(86400, 300 * (2 ** min(failures, 8)))
            next_fetch = now + timedelta(seconds=fail_delay)
            etag = existing.etag if existing else None
            last_mod = existing.last_modified if existing else None
            content_hash = existing.content_hash if existing else None
            status = result.status

        record = URLRecord(
            url=canonical,
            first_seen_at=first_seen,
            last_crawled_at=now,
            status=status,
            status_code=result.status_code,
            etag=etag,
            last_modified=last_mod,
            content_hash=content_hash,
            next_fetch_at=next_fetch,
            consecutive_failures=consecutive_failures,
            crawl_count=prev_crawl_count + (1 if result.is_success else 0),
        )

        await self.state_store.save_record(record)

    async def load_due_requests(self, limit: int = 100) -> int:
        """Poll the persistent state store for URLs whose next_fetch_at has arrived and schedule them."""
        now = datetime.now(timezone.utc)
        due_records = await self.state_store.get_due_records(before=now, limit=limit)
        scheduled_count = 0

        for record in due_records:
            req = CrawlRequest(
                url=record.url,
                depth=0,
                etag=record.etag,
                last_modified=record.last_modified,
                metadata={"recrawl": True, "prev_crawl_count": record.crawl_count},
            )
            # Use force=True because this URL was already seen in a previous crawl cycle
            if self.url_manager.add_request(req, force=True):
                scheduled_count += 1

        return scheduled_count
