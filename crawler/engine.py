"""Crawler acquisition engine coordinating discovery, prioritization, robots, rate limiting, and fetching."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import logging
import time
from typing import Any, AsyncIterator, Callable, Coroutine

from crawler.config import CrawlerConfig
from crawler.discovery import URLDiscoveryService
from crawler.fetcher import AsyncFetcher
from crawler.models import (
    CrawlEvent,
    CrawlEventType,
    CrawlRequest,
    FetchError,
    FetchResult,
    FetchStatus,
)
from crawler.rate_limiter import HostRateLimiter
from crawler.robots import RobotsManager
from crawler.scheduler import CrawlScheduler
from crawler.state import CrawlStateStore, InMemoryCrawlStateStore, SQLiteCrawlStateStore
from crawler.url_manager import URLManager, sanitize_url_for_logging

logger = logging.getLogger(__name__)

EventListener = Callable[[CrawlEvent], Any]
PipelineHandler = Callable[[FetchResult], Any]


class CrawlerEngine:
    """Master Acquisition Engine for the Web Crawler.

    Coordinates Person A's acquisition components and provides clean,
    typed integration boundaries for Person B (Pipeline) and Person C (API/Dashboard).
    """

    def __init__(
        self,
        config: CrawlerConfig | None = None,
        state_store: CrawlStateStore | None = None,
        rate_limiter: HostRateLimiter | None = None,
        fetcher: AsyncFetcher | None = None,
        url_manager: URLManager | None = None,
    ) -> None:
        self.config = config or CrawlerConfig()
        self.url_manager = url_manager or URLManager(self.config)

        if state_store is not None:
            self.state_store = state_store
        elif self.config.state_db_path == ":memory:":
            self.state_store = InMemoryCrawlStateStore()
        else:
            self.state_store = SQLiteCrawlStateStore(self.config.state_db_path)

        self.scheduler = CrawlScheduler(self.config, self.state_store, self.url_manager)
        self.rate_limiter = rate_limiter or HostRateLimiter(self.config)
        self.robots_manager = RobotsManager(self.config)
        self.fetcher = fetcher or AsyncFetcher(self.config, rate_limiter=self.rate_limiter)
        self.discovery_service = URLDiscoveryService(self.config)

        # Integration Handlers
        self._event_listeners: list[EventListener] = []
        self._pipeline_handler: PipelineHandler | None = None

        # Execution Lifecycle Controls
        self._running = False
        self._paused = False
        self._stop_requested = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._workers: list[asyncio.Task[None]] = []

        # Acquisition Metrics and Observability (Person C)
        self.pages_crawled = 0
        self.pages_succeeded = 0
        self.pages_failed = 0
        self.pages_not_modified = 0
        self.pages_robots_blocked = 0
        self.pages_ssrf_blocked = 0
        self.retries_total = 0
        self.urls_discovered = 0
        self.total_response_time_ms = 0.0
        self.start_time: float | None = None

    @property
    def average_response_time_ms(self) -> float:
        """Average latency per crawled page in milliseconds."""
        if self.pages_crawled == 0:
            return 0.0
        return round(self.total_response_time_ms / self.pages_crawled, 2)

    def get_metrics(self) -> dict[str, Any]:
        """Retrieve structured metrics dictionary for monitoring and dashboards (Person C)."""
        uptime_sec = round(time.monotonic() - self.start_time, 2) if self.start_time else 0.0
        return {
            "pages_crawled": self.pages_crawled,
            "pages_succeeded": self.pages_succeeded,
            "pages_failed": self.pages_failed,
            "pages_not_modified": self.pages_not_modified,
            "pages_robots_blocked": self.pages_robots_blocked,
            "pages_ssrf_blocked": self.pages_ssrf_blocked,
            "retries_total": self.retries_total,
            "urls_discovered": self.urls_discovered,
            "queue_pending": len(self.url_manager),
            "uptime_sec": uptime_sec,
            "total_response_time_ms": round(self.total_response_time_ms, 2),
            "average_response_time_ms": self.average_response_time_ms,
            "is_running": self._running,
            "is_paused": self._paused,
        }

    def add_event_listener(self, listener: EventListener) -> None:
        """Register a callback to receive CrawlEvent updates (Person C - Dashboard / Metrics)."""
        self._event_listeners.append(listener)

    def set_pipeline_handler(self, handler: PipelineHandler) -> None:
        """Register the downstream processing handler (Person B - Pipeline / Storage)."""
        self._pipeline_handler = handler

    async def _emit_event(self, event: CrawlEvent) -> None:
        """Broadcast a crawl event to all registered listeners safely."""
        for listener in self._event_listeners:
            try:
                res = listener(event)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.error(f"Error in crawl event listener: {e}", exc_info=True)

    async def _dispatch_result(self, result: FetchResult) -> None:
        """Hand off a FetchResult to Person B's pipeline handler."""
        if self._pipeline_handler is not None:
            try:
                res = self._pipeline_handler(result)
                if inspect.isawaitable(res):
                    await res
            except Exception as e:
                logger.error(f"Error in pipeline handler: {e}", exc_info=True)

    async def add_seed_url(
        self,
        url: str,
        priority: float = 10.0,
        depth: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Add an initial seed URL to the crawler priority queue."""
        req = CrawlRequest(
            url=url,
            priority=priority,
            depth=depth,
            metadata=metadata or {},
        )
        scheduled = await self.scheduler.schedule(req, force=True)
        if scheduled:
            self.urls_discovered += 1
            await self._emit_event(
                CrawlEvent(
                    event_type=CrawlEventType.URL_DISCOVERED,
                    url=req.url,
                    details={"seed": True, "depth": depth},
                )
            )
        return scheduled

    async def add_seed_urls(self, urls: list[str]) -> int:
        """Add multiple seed URLs in bulk."""
        count = 0
        for u in urls:
            if await self.add_seed_url(u):
                count += 1
        return count

    async def stop(self) -> None:
        """Signal all workers to stop gracefully."""
        self._running = False
        self._stop_requested.set()
        self._resume_event.set()
        for w in self._workers:
            if not w.done():
                w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def pause(self) -> None:
        """Pause worker processing without shutting down connections."""
        self._paused = True
        self._resume_event.clear()
        logger.info("CrawlerEngine paused")

    def resume(self) -> None:
        """Resume worker processing."""
        self._paused = False
        self._resume_event.set()
        logger.info("CrawlerEngine resumed")

    @property
    def is_running(self) -> bool:
        return self._running

    async def run(self) -> None:
        """Run the crawler until the queue is exhausted or max_pages limit is reached."""
        self._running = True
        self._stop_requested.clear()
        self._resume_event.set()
        self.start_time = time.monotonic()

        await self.scheduler.initialize()

        # Spawn worker tasks up to max_concurrency
        concurrency = max(1, self.config.max_concurrency)
        self._workers = [
            asyncio.create_task(self._worker_loop(worker_id=i))
            for i in range(concurrency)
        ]

        try:
            await asyncio.gather(*self._workers)
        except asyncio.CancelledError:
            pass
        finally:
            await self.close()
            self._running = False

    async def run_generator(self) -> AsyncIterator[FetchResult]:
        """Run crawler while yielding FetchResults as an asynchronous stream for Person B."""
        result_queue: asyncio.Queue[FetchResult | None] = asyncio.Queue()

        async def queue_pipeline_handler(res: FetchResult) -> None:
            await result_queue.put(res)

        self.set_pipeline_handler(queue_pipeline_handler)

        crawler_task = asyncio.create_task(self.run())

        try:
            while True:
                # If crawler task finished and queue is empty, break
                if crawler_task.done() and result_queue.empty():
                    # Propagate exception if crawler failed
                    if crawler_task.exception():
                        raise crawler_task.exception()  # type: ignore
                    break

                try:
                    res = await asyncio.wait_for(result_queue.get(), timeout=0.2)
                    if res is not None:
                        yield res
                except asyncio.TimeoutError:
                    continue
        finally:
            if not crawler_task.done():
                await self.stop()
                await crawler_task

    async def _worker_loop(self, worker_id: int) -> None:
        """Individual worker pulling requests from priority queue and executing fetch sequence."""
        idle_count = 0
        max_idle_polls = 10

        while self._running and not self._stop_requested.is_set():
            # Respect pause state
            await self._resume_event.wait()

            # Check max pages constraint
            if self.pages_crawled >= self.config.max_pages:
                logger.info(f"Reached max pages limit ({self.config.max_pages}). Worker {worker_id} exiting.")
                break

            req = self.url_manager.pop_request()
            if req is None:
                # Check if there are any recrawl requests due in persistent store
                due_count = await self.scheduler.load_due_requests(limit=10)
                if due_count > 0:
                    req = self.url_manager.pop_request()

            if req is None:
                idle_count += 1
                if idle_count >= max_idle_polls:
                    # Queue is empty and no work pending
                    break
                await asyncio.sleep(0.1)
                continue

            idle_count = 0
            await self._process_request(req)

    async def _process_request(self, req: CrawlRequest) -> None:
        """Process a single CrawlRequest through robots check, fetch, discovery, and handoff."""
        # 1. Robots.txt Compliance Check (RFC 9309)
        can_fetch = await self.robots_manager.can_fetch(
            req.url,
            fetcher_callable=self.fetcher.fetch_robots_txt,
        )

        if not can_fetch:
            self.pages_robots_blocked += 1
            result = FetchResult(
                requested_url=req.url,
                final_url=req.url,
                status=FetchStatus.ROBOTS_BLOCKED,
                error=FetchError(
                    error_type="RobotsBlocked",
                    message="Blocked by site robots.txt policy",
                ),
                depth=req.depth,
                metadata=req.metadata,
            )
            await self._emit_event(
                CrawlEvent(
                    event_type=CrawlEventType.ROBOTS_BLOCKED,
                    url=req.url,
                    message="Disallowed by robots.txt",
                )
            )
            await self.scheduler.record_outcome(result)
            await self._dispatch_result(result)
            return

        # 2. Emit Fetch Start Event
        await self._emit_event(
            CrawlEvent(
                event_type=CrawlEventType.FETCH_START,
                url=req.url,
                details={"depth": req.depth, "retry_count": req.retry_count},
            )
        )

        # 3. Fetch Resource
        crawl_delay = self.robots_manager.get_crawl_delay(req.url)
        result = await self.fetcher.fetch(req, crawl_delay=crawl_delay)
        self.pages_crawled += 1
        self.retries_total += result.retry_count
        self.total_response_time_ms += result.latency_ms

        # 4. Handle Outcome and Emit Status Events
        if result.status == FetchStatus.SSRF_BLOCKED:
            self.pages_ssrf_blocked += 1
            self.pages_failed += 1
            await self._emit_event(
                CrawlEvent(
                    event_type=CrawlEventType.FAILURE,
                    url=result.final_url,
                    latency_ms=result.latency_ms,
                    retry_count=result.retry_count,
                    message="Blocked by SSRF security check",
                )
            )
        elif result.not_modified:
            self.pages_not_modified += 1
            await self._emit_event(
                CrawlEvent(
                    event_type=CrawlEventType.NOT_MODIFIED,
                    url=result.final_url,
                    status_code=304,
                    latency_ms=result.latency_ms,
                    retry_count=result.retry_count,
                )
            )
        elif result.is_success:
            self.pages_succeeded += 1
            await self._emit_event(
                CrawlEvent(
                    event_type=CrawlEventType.FETCH_SUCCESS,
                    url=result.final_url,
                    status_code=result.status_code,
                    latency_ms=result.latency_ms,
                    retry_count=result.retry_count,
                    details={"content_type": result.content_type},
                )
            )
        else:
            self.pages_failed += 1
            await self._emit_event(
                CrawlEvent(
                    event_type=CrawlEventType.FAILURE,
                    url=result.final_url,
                    status_code=result.status_code,
                    latency_ms=result.latency_ms,
                    retry_count=result.retry_count,
                    message=result.error.message if result.error else "Fetch failed",
                )
            )

        # 5. Discover New URLs if HTML/XML was retrieved
        if result.is_success and req.depth < self.config.max_depth:
            discovered_requests = self.discovery_service.discover_from_result(
                result=result,
                current_depth=req.depth,
                allowed_domains=req.allowed_domains or self.config.allowed_domains,
            )
            for new_req in discovered_requests:
                scheduled = await self.scheduler.schedule(new_req)
                if scheduled:
                    self.urls_discovered += 1
                    await self._emit_event(
                        CrawlEvent(
                            event_type=CrawlEventType.URL_DISCOVERED,
                            url=new_req.url,
                            details={"parent_url": result.final_url, "depth": new_req.depth},
                        )
                    )

        # 6. Record State & Persist
        await self.scheduler.record_outcome(result)

        # 7. Hand Off to Person B Pipeline
        await self._dispatch_result(result)

    async def close(self) -> None:
        """Shut down resources, sessions, and state stores."""
        await self.fetcher.close()
        await self.state_store.close()
