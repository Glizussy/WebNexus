"""Per-host rate limiting, Retry-After parsing/capping, and distributed state coordination."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import time
from typing import Protocol
from urllib.parse import urlsplit

from crawler.config import CrawlerConfig

logger = logging.getLogger(__name__)


def parse_retry_after(retry_after_header: str | None, max_cap_sec: float = 60.0) -> float | None:
    """Parse HTTP Retry-After header into delay in seconds.

    Supports both:
    1. Integer seconds: e.g. '120'
    2. HTTP-date: e.g. 'Fri, 31 Dec 2026 23:59:59 GMT'

    Capped at max_cap_sec to prevent denial-of-service from malicious headers.
    Returns None if missing or unparseable.
    """
    if not retry_after_header or not retry_after_header.strip():
        return None

    raw = retry_after_header.strip()

    # Case 1: Integer seconds
    try:
        seconds = float(raw)
        if seconds < 0:
            return 0.0
        return min(seconds, max_cap_sec)
    except ValueError:
        pass

    # Case 2: HTTP-date format
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (dt - now).total_seconds()
        if delta < 0:
            return 0.0
        return min(delta, max_cap_sec)
    except Exception as e:
        logger.debug(f"Failed to parse Retry-After header '{raw}': {e}")
        return None


class RateLimiterBackend(Protocol):
    """Protocol for pluggable rate limiter state stores (In-Memory or Distributed/Redis)."""

    async def wait_for_slot(self, host: str, required_delay_sec: float) -> float:
        """Wait until a slot is available for host respecting required_delay_sec.

        Returns actual seconds waited.
        """
        ...

    async def record_retry_after(self, host: str, retry_after_sec: float) -> None:
        """Enforce a mandatory pause on host until retry_after_sec has elapsed."""
        ...


class InMemoryRateLimiterBackend:
    """Thread-safe and async-safe in-memory rate limiting backend using per-host locks and timestamps."""

    def __init__(self) -> None:
        self._last_access: dict[str, float] = {}
        self._retry_until: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_host_lock(self, host: str) -> asyncio.Lock:
        async with self._global_lock:
            if host not in self._host_locks:
                self._host_locks[host] = asyncio.Lock()
            return self._host_locks[host]

    async def record_retry_after(self, host: str, retry_after_sec: float) -> None:
        host = host.lower()
        now = time.monotonic()
        target = now + max(0.0, retry_after_sec)
        async with self._global_lock:
            current = self._retry_until.get(host, 0.0)
            if target > current:
                self._retry_until[host] = target

    async def wait_for_slot(self, host: str, required_delay_sec: float) -> float:
        host = host.lower()
        lock = await self._get_host_lock(host)
        waited_total = 0.0

        async with lock:
            now = time.monotonic()

            # Check Retry-After block
            retry_target = self._retry_until.get(host, 0.0)
            if retry_target > now:
                delay = retry_target - now
                await asyncio.sleep(delay)
                waited_total += delay
                now = time.monotonic()

            # Check per-host rate limit delay
            last = self._last_access.get(host, 0.0)
            elapsed = now - last
            if elapsed < required_delay_sec:
                wait_time = required_delay_sec - elapsed
                await asyncio.sleep(wait_time)
                waited_total += wait_time

            # Update last access timestamp
            self._last_access[host] = time.monotonic()

        return waited_total


class HostRateLimiter:
    """Rate limiter enforcing per-host crawl delays and Retry-After headers."""

    def __init__(
        self,
        config: CrawlerConfig,
        backend: RateLimiterBackend | None = None,
    ) -> None:
        self.config = config
        self.backend: RateLimiterBackend = backend or InMemoryRateLimiterBackend()

    @staticmethod
    def extract_host(url: str) -> str:
        """Extract host identifier (host:port) from a URL."""
        split = urlsplit(url)
        return split.netloc.lower() or "unknown_host"

    async def throttle(self, url: str, crawl_delay: float | None = None) -> float:
        """Throttle execution for the host of URL.

        Applies the most restrictive applicable configured limit:
        max(config.per_host_delay_sec, crawl_delay if respect_crawl_delay).
        """
        host = self.extract_host(url)
        effective_delay = self.config.per_host_delay_sec

        if self.config.respect_crawl_delay and crawl_delay is not None:
            capped_delay = min(crawl_delay, self.config.max_crawl_delay_sec)
            effective_delay = max(effective_delay, capped_delay)

        return await self.backend.wait_for_slot(host, effective_delay)

    async def handle_retry_after(self, url: str, retry_after_header: str | None) -> float | None:
        """Record and enforce a Retry-After delay for the host of URL."""
        seconds = parse_retry_after(
            retry_after_header,
            max_cap_sec=self.config.max_retry_after_sec,
        )
        if seconds is not None and seconds > 0:
            host = self.extract_host(url)
            await self.backend.record_retry_after(host, seconds)
        return seconds
