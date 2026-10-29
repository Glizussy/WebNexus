"""Unit tests for per-host rate limiting, Retry-After parsing and capping."""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import time
import pytest
from crawler.config import CrawlerConfig
from crawler.rate_limiter import HostRateLimiter, parse_retry_after


def test_parse_retry_after_seconds():
    assert parse_retry_after("15", max_cap_sec=60) == 15.0
    assert parse_retry_after("120", max_cap_sec=60) == 60.0  # Capped
    assert parse_retry_after("-5", max_cap_sec=60) == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("invalid") is None


def test_parse_retry_after_http_date():
    future_time = datetime.now(timezone.utc) + timedelta(seconds=20)
    date_str = format_datetime(future_time)
    parsed = parse_retry_after(date_str, max_cap_sec=60)
    assert parsed is not None
    assert 18.0 <= parsed <= 22.0


@pytest.mark.asyncio
async def test_host_rate_limiter_delay():
    cfg = CrawlerConfig(per_host_delay_sec=0.1)
    limiter = HostRateLimiter(cfg)

    t0 = time.monotonic()
    # First request goes through with minimal delay
    await limiter.throttle("https://example.com/page1")
    # Second request to same host must wait ~0.1s
    await limiter.throttle("https://example.com/page2")
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.09


@pytest.mark.asyncio
async def test_host_rate_limiter_honors_retry_after():
    cfg = CrawlerConfig(per_host_delay_sec=0.01, max_retry_after_sec=5.0)
    limiter = HostRateLimiter(cfg)

    # Record 0.15s retry-after
    await limiter.handle_retry_after("https://busy.org/api", "0.15")

    t0 = time.monotonic()
    await limiter.throttle("https://busy.org/item")
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.14
