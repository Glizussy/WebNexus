"""Unit tests for persistent crawl state storage and the incremental CrawlScheduler."""

from datetime import datetime, timedelta, timezone
import os
import tempfile
import pytest
from crawler.config import CrawlerConfig
from crawler.models import CrawlRequest, FetchResult, FetchStatus
from crawler.scheduler import CrawlScheduler
from crawler.state import InMemoryCrawlStateStore, SQLiteCrawlStateStore, compute_content_hash
from crawler.url_manager import URLManager


@pytest.mark.asyncio
async def test_sqlite_state_store_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_crawl.db")
        store = SQLiteCrawlStateStore(db_path)

        # 1. Setup scheduler and record an outcome
        cfg = CrawlerConfig(state_db_path=db_path, default_recrawl_interval_sec=3600)
        url_manager = URLManager(cfg)
        scheduler = CrawlScheduler(cfg, store, url_manager)

        res = FetchResult(
            requested_url="https://example.com/article",
            final_url="https://example.com/article",
            status=FetchStatus.SUCCESS,
            status_code=200,
            body=b"Hello World Content",
            etag='"xyz-999"',
            last_modified="Wed, 21 Oct 2026 07:28:00 GMT",
        )
        await scheduler.record_outcome(res)
        await store.close()

        # 2. Simulate complete application restart with a new store instance
        store2 = SQLiteCrawlStateStore(db_path)
        record = await store2.get_record("https://example.com/article")

        assert record is not None
        assert record.url == "https://example.com/article"
        assert record.etag == '"xyz-999"'
        assert record.last_modified == "Wed, 21 Oct 2026 07:28:00 GMT"
        assert record.crawl_count == 1
        assert record.content_hash == compute_content_hash(b"Hello World Content")
        await store2.close()


@pytest.mark.asyncio
async def test_scheduler_attaches_conditional_validators():
    store = InMemoryCrawlStateStore()
    cfg = CrawlerConfig()
    url_mgr = URLManager(cfg)
    scheduler = CrawlScheduler(cfg, store, url_mgr)

    # Initial crawl outcome
    res = FetchResult(
        requested_url="https://example.com/data",
        final_url="https://example.com/data",
        status=FetchStatus.SUCCESS,
        status_code=200,
        etag='"etag-v1"',
        last_modified="Tue, 20 Oct 2026 12:00:00 GMT",
    )
    # Schedule with 0 recrawl interval so it is immediately eligible
    await scheduler.record_outcome(res, recrawl_interval_sec=0)

    # Schedule a new request for the same URL
    req = CrawlRequest("https://example.com/data")
    scheduled = await scheduler.schedule(req, force=True)

    assert scheduled is True
    assert req.etag == '"etag-v1"'
    assert req.last_modified == "Tue, 20 Oct 2026 12:00:00 GMT"


@pytest.mark.asyncio
async def test_scheduler_304_preserves_validators():
    store = InMemoryCrawlStateStore()
    cfg = CrawlerConfig()
    url_mgr = URLManager(cfg)
    scheduler = CrawlScheduler(cfg, store, url_mgr)

    # First fetch: 200 with content
    res1 = FetchResult(
        requested_url="https://example.com/item",
        final_url="https://example.com/item",
        status=FetchStatus.SUCCESS,
        status_code=200,
        body=b"Original Body",
        etag='"etag-original"',
    )
    await scheduler.record_outcome(res1)

    # Second fetch: 304 with no body and server omits etag header
    res2 = FetchResult(
        requested_url="https://example.com/item",
        final_url="https://example.com/item",
        status=FetchStatus.NOT_MODIFIED,
        status_code=304,
        not_modified=True,
        body=None,
        etag=None,
    )
    await scheduler.record_outcome(res2)

    rec = await store.get_record("https://example.com/item")
    assert rec is not None
    assert rec.status == FetchStatus.NOT_MODIFIED
    # Preserved from initial fetch!
    assert rec.etag == '"etag-original"'
    assert rec.content_hash == compute_content_hash(b"Original Body")


@pytest.mark.asyncio
async def test_scheduler_prevents_premature_recrawl():
    store = InMemoryCrawlStateStore()
    cfg = CrawlerConfig()
    url_mgr = URLManager(cfg)
    scheduler = CrawlScheduler(cfg, store, url_mgr)

    res = FetchResult(
        requested_url="https://example.com/page",
        final_url="https://example.com/page",
        status=FetchStatus.SUCCESS,
        status_code=200,
    )
    # Recrawl scheduled 1 hour in the future
    await scheduler.record_outcome(res, recrawl_interval_sec=3600)

    # Attempt to schedule without force should be rejected
    req = CrawlRequest("https://example.com/page")
    scheduled = await scheduler.schedule(req, force=False)
    assert scheduled is False
