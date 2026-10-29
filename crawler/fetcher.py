"""Asynchronous HTTP fetcher with connection pooling, retries, exponential backoff, jitter, and SSRF protection."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import random
import time
from typing import Any
from urllib.parse import urljoin

import aiohttp

from crawler.config import CrawlerConfig
from crawler.models import (
    CrawlRequest,
    FetchError,
    FetchResult,
    FetchStatus,
)
from crawler.rate_limiter import HostRateLimiter, parse_retry_after
from crawler.ssrf import SafeSSRFResolver, SSRFSecurityError, validate_destination_async

logger = logging.getLogger(__name__)

# HTTP status codes that are considered transient and eligible for bounded retry
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _get_header(headers: dict[str, str], key: str, default: str | None = None) -> str | None:
    """Case-insensitive header retrieval."""
    key_lower = key.lower()
    for k, v in headers.items():
        if k.lower() == key_lower:
            return v
    return default


class AsyncFetcher:
    """High-performance asynchronous HTTP fetcher adhering to strict security and acquisition policies."""

    def __init__(
        self,
        config: CrawlerConfig,
        rate_limiter: HostRateLimiter | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.config = config
        self.rate_limiter = rate_limiter or HostRateLimiter(config)
        self._custom_session = session is not None
        self._session: aiohttp.ClientSession | None = session
        self._semaphore = asyncio.Semaphore(config.max_concurrency)
        self._closed = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                sock_connect=self.config.connect_timeout_sec,
                total=self.config.request_timeout_sec,
            )
            resolver = SafeSSRFResolver(
                allow_private_ips=self.config.allow_private_ips,
                blocked_hostnames=self.config.blocked_hostnames,
            )
            connector = aiohttp.TCPConnector(
                limit=self.config.max_concurrency,
                limit_per_host=self.config.max_connections_per_host,
                enable_cleanup_closed=True,
                force_close=False,
                resolver=resolver,
            )
            headers = {
                "User-Agent": self.config.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            }
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers=headers,
            )
        return self._session

    async def close(self) -> None:
        """Gracefully close the underlying HTTP session and connection pools."""
        self._closed = True
        if self._session and not self._session.closed and not self._custom_session:
            await self._session.close()
            # Allow underlying SSL transports to close cleanly
            await asyncio.sleep(0.05)

    async def __aenter__(self) -> AsyncFetcher:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    def _compute_backoff(self, attempt: int, retry_after: float | None = None) -> float:
        """Compute exponential backoff with full jitter, honoring capped Retry-After if greater."""
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.config.max_retry_after_sec)

        base = self.config.backoff_factor ** attempt
        jitter = random.uniform(0.0, 1.0) if self.config.backoff_jitter else 0.0
        backoff = min(self.config.max_backoff_sec, base + jitter)
        return max(0.1, backoff)

    async def fetch_robots_txt(self, robots_url: str) -> tuple[int, str | None]:
        """Fetch robots.txt for an origin returning (status_code, text_content).

        Enforces:
        - Per-hop SSRF validation for initial URL and all redirect destinations.
        - Redirect loop detection and redirect limit enforcement.
        - Strict streaming response-size enforcement (max_robots_size_bytes).
        - Rejection of oversized responses (returns 413) so truncated policies are never used.
        - Safe handling of missing, misleading, or oversized Content-Length headers.
        - Accounts for decompressed body bytes during streaming.
        """
        session = await self._get_session()
        current_url = robots_url
        redirect_history: set[str] = {current_url}
        hops = 0

        while hops <= self.config.max_redirects:
            # 1. Per-hop destination SSRF check
            try:
                await validate_destination_async(
                    current_url,
                    allow_private_ips=self.config.allow_private_ips,
                    blocked_hostnames=self.config.blocked_hostnames,
                )
            except SSRFSecurityError as e:
                logger.warning(f"SSRF blocked robots.txt request to {current_url}: {e}")
                return 599, None

            timeout = aiohttp.ClientTimeout(total=self.config.robots_timeout_sec)
            try:
                async with session.get(
                    current_url,
                    timeout=timeout,
                    allow_redirects=False,  # Never allow blind redirects!
                ) as resp:
                    status = resp.status
                    headers = dict(resp.headers)

                    # Handle redirects (301, 302, 303, 307, 308)
                    if status in (301, 302, 303, 307, 308):
                        location = _get_header(headers, "Location")
                        if not location:
                            logger.warning(f"Robots redirect from {current_url} missing Location header")
                            return 599, None

                        next_url = urljoin(current_url, location.strip())
                        hops += 1

                        # Detect redirect loop
                        if next_url in redirect_history:
                            logger.warning(f"Redirect loop detected fetching robots.txt: {next_url}")
                            return 599, None

                        # Detect excessive redirects
                        if hops > self.config.max_redirects:
                            logger.warning(
                                f"Exceeded max redirects ({self.config.max_redirects}) fetching robots.txt from {robots_url}"
                            )
                            return 599, None

                        redirect_history.add(next_url)
                        current_url = next_url
                        continue

                    # Non-redirect response:
                    # 404/410: RFC 9309 says resource doesn't exist -> allow all
                    if status == 404 or status == 410:
                        return status, None
                    # Other non-200 status codes:
                    if status != 200:
                        return status, None

                    # Enforce max_robots_size_bytes on Content-Length header if present
                    cl_header = _get_header(headers, "Content-Length")
                    if cl_header:
                        try:
                            cl = int(cl_header)
                            if cl > self.config.max_robots_size_bytes:
                                logger.warning(
                                    f"Robots.txt Content-Length ({cl}) exceeds limit ({self.config.max_robots_size_bytes})"
                                )
                                return 413, None
                        except ValueError:
                            pass

                    # Stream response body chunk-by-chunk to enforce limit even if Content-Length is missing or misleading
                    body_chunks: list[bytes] = []
                    bytes_read = 0
                    oversized = False

                    async for chunk in resp.content.iter_chunked(16384):
                        bytes_read += len(chunk)
                        if bytes_read > self.config.max_robots_size_bytes:
                            oversized = True
                            break
                        body_chunks.append(chunk)

                    if oversized:
                        logger.warning(
                            f"Robots.txt from {current_url} exceeded max size ({self.config.max_robots_size_bytes} bytes). Aborting."
                        )
                        return 413, None

                    full_body = b"".join(body_chunks)
                    encoding = resp.charset or "utf-8"
                    try:
                        text_content = full_body.decode(encoding, errors="replace")
                    except Exception:
                        text_content = full_body.decode("utf-8", errors="replace")

                    return 200, text_content

            except (aiohttp.ClientError, asyncio.TimeoutError, SSRFSecurityError) as e:
                logger.debug(f"Network error or timeout fetching robots.txt from {current_url}: {e}")
                return 599, None
            except Exception as e:
                logger.warning(f"Unexpected error fetching robots.txt: {e}")
                return 599, None

        return 599, None

    async def fetch(
        self,
        request: CrawlRequest,
        crawl_delay: float | None = None,
    ) -> FetchResult:
        """Execute a full fetch sequence for a CrawlRequest including retries, rate limiting, and redirects."""
        if self._closed:
            return FetchResult(
                requested_url=request.url,
                final_url=request.url,
                status=FetchStatus.CANCELLED,
                error=FetchError(error_type="Cancelled", message="Fetcher is closed"),
                depth=request.depth,
                metadata=request.metadata,
            )

        start_time = time.monotonic()
        retry_count = 0
        current_url = request.url

        # Outer retry loop for transient failures (429, 5xx, network drops, timeouts)
        while True:
            try:
                async with self._semaphore:
                    # Enforce host-level rate limiting before network connection
                    await self.rate_limiter.throttle(current_url, crawl_delay=crawl_delay)
                    return await self._execute_request_with_redirects(
                        request=request,
                        start_time=start_time,
                        retry_count=retry_count,
                    )
            except SSRFSecurityError as e:
                # SSRF violations are security blocks and must never be retried
                latency_ms = (time.monotonic() - start_time) * 1000.0
                logger.warning(f"SSRF blocked request to {current_url}: {e}")
                return FetchResult(
                    requested_url=request.url,
                    final_url=current_url,
                    status=FetchStatus.SSRF_BLOCKED,
                    error=FetchError(error_type="SSRFSecurityError", message=str(e), retryable=False),
                    retry_count=retry_count,
                    latency_ms=latency_ms,
                    depth=request.depth,
                    metadata=request.metadata,
                )
            except asyncio.CancelledError:
                logger.info(f"Fetch cancelled for {current_url}")
                raise
            except _TransientFetchError as e:
                retry_count += 1
                if retry_count > self.config.max_retries:
                    latency_ms = (time.monotonic() - start_time) * 1000.0
                    logger.warning(f"Exhausted retries ({self.config.max_retries}) for {request.url}: {e.message}")
                    return FetchResult(
                        requested_url=request.url,
                        final_url=e.current_url or current_url,
                        status=e.status,
                        status_code=e.status_code,
                        error=FetchError(
                            error_type=e.error_type,
                            message=e.message,
                            status_code=e.status_code,
                            retryable=False,
                        ),
                        retry_count=retry_count - 1,
                        latency_ms=latency_ms,
                        depth=request.depth,
                        metadata=request.metadata,
                    )

                backoff_wait = self._compute_backoff(retry_count, e.retry_after)
                logger.info(
                    f"Transient error fetching {current_url} ({e.error_type}: {e.message}). "
                    f"Retrying ({retry_count}/{self.config.max_retries}) in {backoff_wait:.2f}s..."
                )
                await asyncio.sleep(backoff_wait)

            except Exception as e:
                # Non-retryable unexpected fatal error
                latency_ms = (time.monotonic() - start_time) * 1000.0
                logger.error(f"Non-retryable error fetching {request.url}: {e}", exc_info=True)
                return FetchResult(
                    requested_url=request.url,
                    final_url=current_url,
                    status=FetchStatus.NETWORK_ERROR,
                    error=FetchError(error_type=type(e).__name__, message=str(e), retryable=False),
                    retry_count=retry_count,
                    latency_ms=latency_ms,
                    depth=request.depth,
                    metadata=request.metadata,
                )

    async def _execute_request_with_redirects(
        self,
        request: CrawlRequest,
        start_time: float,
        retry_count: int,
    ) -> FetchResult:
        """Handle request execution including manual redirect following to validate SSRF at every hop."""
        session = await self._get_session()
        current_url = request.url
        redirect_history: list[str] = [current_url]
        hops = 0

        while hops <= self.config.max_redirects:
            # 1. Validate destination IP before making connection (SSRF check)
            await validate_destination_async(
                current_url,
                allow_private_ips=self.config.allow_private_ips,
                blocked_hostnames=self.config.blocked_hostnames,
            )

            # 2. Build conditional request headers
            req_headers: dict[str, str] = {}
            if request.etag:
                req_headers["If-None-Match"] = request.etag
            if request.last_modified:
                req_headers["If-Modified-Since"] = request.last_modified

            # 3. Make HTTP request with allow_redirects=False to inspect each redirect hop
            try:
                async with session.get(
                    current_url,
                    headers=req_headers,
                    allow_redirects=False,
                ) as resp:
                    resp_headers = dict(resp.headers)
                    status_code = resp.status

                    # Handle Redirects (301, 302, 303, 307, 308)
                    if status_code in (301, 302, 303, 307, 308):
                        location = _get_header(resp_headers, "Location")
                        if not location:
                            # Redirect header missing Location
                            latency_ms = (time.monotonic() - start_time) * 1000.0
                            return FetchResult(
                                requested_url=request.url,
                                final_url=current_url,
                                status=FetchStatus.HTTP_ERROR,
                                status_code=status_code,
                                headers=resp_headers,
                                latency_ms=latency_ms,
                                depth=request.depth,
                                metadata=request.metadata,
                            )

                        next_url = urljoin(current_url, location.strip())
                        hops += 1

                        # Detect redirect loop immediately
                        if next_url in redirect_history:
                            latency_ms = (time.monotonic() - start_time) * 1000.0
                            return FetchResult(
                                requested_url=request.url,
                                final_url=current_url,
                                status=FetchStatus.TOO_MANY_REDIRECTS,
                                status_code=status_code,
                                headers=resp_headers,
                                error=FetchError(
                                    error_type="RedirectLoop",
                                    message=f"Redirect loop detected: {next_url} already visited",
                                ),
                                retry_count=retry_count,
                                latency_ms=latency_ms,
                                depth=request.depth,
                                metadata=request.metadata,
                            )

                        if hops > self.config.max_redirects:
                            latency_ms = (time.monotonic() - start_time) * 1000.0
                            return FetchResult(
                                requested_url=request.url,
                                final_url=current_url,
                                status=FetchStatus.TOO_MANY_REDIRECTS,
                                status_code=status_code,
                                headers=resp_headers,
                                error=FetchError(
                                    error_type="TooManyRedirects",
                                    message=f"Exceeded max redirects limit of {self.config.max_redirects}",
                                ),
                                retry_count=retry_count,
                                latency_ms=latency_ms,
                                depth=request.depth,
                                metadata=request.metadata,
                            )

                        redirect_history.append(next_url)
                        current_url = next_url
                        # Throttle next redirect hop and continue loop
                        await self.rate_limiter.throttle(current_url)
                        continue

                    # Handle 304 Not Modified
                    if status_code == 304:
                        latency_ms = (time.monotonic() - start_time) * 1000.0
                        # Preserve existing validators if server omitted them
                        etag = _get_header(resp_headers, "ETag") or request.etag
                        last_mod = _get_header(resp_headers, "Last-Modified") or request.last_modified
                        return FetchResult(
                            requested_url=request.url,
                            final_url=current_url,
                            status=FetchStatus.NOT_MODIFIED,
                            status_code=304,
                            headers=resp_headers,
                            not_modified=True,
                            etag=etag,
                            last_modified=last_mod,
                            retry_count=retry_count,
                            latency_ms=latency_ms,
                            depth=request.depth,
                            metadata=request.metadata,
                        )

                    # Handle 429 Too Many Requests
                    if status_code == 429:
                        retry_after_header = _get_header(resp_headers, "Retry-After")
                        parsed_wait = await self.rate_limiter.handle_retry_after(
                            current_url, retry_after_header
                        )
                        raise _TransientFetchError(
                            status=FetchStatus.HTTP_ERROR,
                            status_code=429,
                            error_type="HTTP_429_RateLimited",
                            message=f"Rate limited by server (Retry-After: {retry_after_header})",
                            current_url=current_url,
                            retry_after=parsed_wait,
                        )

                    # Handle 5xx Server Errors (Transient)
                    if status_code in (500, 502, 503, 504):
                        retry_after_header = _get_header(resp_headers, "Retry-After")
                        parsed_wait = await self.rate_limiter.handle_retry_after(
                            current_url, retry_after_header
                        )
                        raise _TransientFetchError(
                            status=FetchStatus.HTTP_ERROR,
                            status_code=status_code,
                            error_type=f"HTTP_{status_code}_ServerError",
                            message=f"Transient server error status {status_code}",
                            current_url=current_url,
                            retry_after=parsed_wait,
                        )

                    # Handle other 4xx Client Errors (Non-Transient)
                    if 400 <= status_code < 500:
                        latency_ms = (time.monotonic() - start_time) * 1000.0
                        return FetchResult(
                            requested_url=request.url,
                            final_url=current_url,
                            status=FetchStatus.HTTP_ERROR,
                            status_code=status_code,
                            headers=resp_headers,
                            error=FetchError(
                                error_type=f"HTTP_{status_code}",
                                message=f"Client error HTTP {status_code}",
                                status_code=status_code,
                                retryable=False,
                            ),
                            retry_count=retry_count,
                            latency_ms=latency_ms,
                            depth=request.depth,
                            metadata=request.metadata,
                        )

                    # Inspect Content-Type
                    content_type = resp.content_type or ""
                    if not any(allowed in content_type.lower() for allowed in self.config.allowed_content_types):
                        latency_ms = (time.monotonic() - start_time) * 1000.0
                        return FetchResult(
                            requested_url=request.url,
                            final_url=current_url,
                            status=FetchStatus.UNSUPPORTED_CONTENT,
                            status_code=status_code,
                            headers=resp_headers,
                            content_type=content_type,
                            error=FetchError(
                                error_type="UnsupportedContentType",
                                message=f"MIME type '{content_type}' is not allowed",
                            ),
                            retry_count=retry_count,
                            latency_ms=latency_ms,
                            depth=request.depth,
                            metadata=request.metadata,
                        )

                    # Stream response with maximum size enforcement
                    body_chunks: list[bytes] = []
                    bytes_read = 0
                    oversized = False

                    async for chunk in resp.content.iter_chunked(65536):
                        bytes_read += len(chunk)
                        if bytes_read > self.config.max_response_size_bytes:
                            oversized = True
                            break
                        body_chunks.append(chunk)

                    latency_ms = (time.monotonic() - start_time) * 1000.0

                    if oversized:
                        return FetchResult(
                            requested_url=request.url,
                            final_url=current_url,
                            status=FetchStatus.OVERSIZED_RESPONSE,
                            status_code=status_code,
                            headers=resp_headers,
                            content_type=content_type,
                            error=FetchError(
                                error_type="OversizedResponse",
                                message=(
                                    f"Response exceeded maximum limit of {self.config.max_response_size_bytes} bytes"
                                ),
                            ),
                            retry_count=retry_count,
                            latency_ms=latency_ms,
                            depth=request.depth,
                            metadata=request.metadata,
                        )

                    full_body = b"".join(body_chunks)
                    encoding = resp.charset or "utf-8"
                    try:
                        text_content = full_body.decode(encoding, errors="replace")
                    except Exception:
                        text_content = full_body.decode("utf-8", errors="replace")

                    etag = _get_header(resp_headers, "ETag")
                    last_mod = _get_header(resp_headers, "Last-Modified")

                    return FetchResult(
                        requested_url=request.url,
                        final_url=current_url,
                        status=FetchStatus.SUCCESS,
                        status_code=status_code,
                        headers=resp_headers,
                        body=full_body,
                        text=text_content,
                        content_type=content_type,
                        encoding=encoding,
                        etag=etag,
                        last_modified=last_mod,
                        retry_count=retry_count,
                        latency_ms=latency_ms,
                        depth=request.depth,
                        metadata=request.metadata,
                    )

            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                aiohttp.ServerTimeoutError,
                asyncio.TimeoutError,
            ) as e:
                raise _TransientFetchError(
                    status=FetchStatus.TIMEOUT if isinstance(e, asyncio.TimeoutError) else FetchStatus.NETWORK_ERROR,
                    status_code=None,
                    error_type=type(e).__name__,
                    message=str(e),
                    current_url=current_url,
                ) from e

        # Exceeded loop without returning
        return FetchResult(
            requested_url=request.url,
            final_url=current_url,
            status=FetchStatus.TOO_MANY_REDIRECTS,
            depth=request.depth,
            metadata=request.metadata,
        )


class _TransientFetchError(Exception):
    """Internal exception for triggering retry loops on transient HTTP/network failures."""

    def __init__(
        self,
        status: FetchStatus,
        status_code: int | None,
        error_type: str,
        message: str,
        current_url: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.current_url = current_url
        self.retry_after = retry_after
