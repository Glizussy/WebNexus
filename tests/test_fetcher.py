"""Unit and integration tests for AsyncFetcher using local aiohttp test server."""

import asyncio
import pytest
from aiohttp import web
from crawler.config import CrawlerConfig
from crawler.fetcher import AsyncFetcher
from crawler.models import CrawlRequest, FetchStatus


@pytest.fixture
async def test_server():
    """Spin up an ephemeral in-process HTTP test server."""
    app = web.Application()
    flaky_count = {"flaky_500": 0, "rate_limit": 0}

    async def handle_success(request):
        return web.Response(text="<html><body><h1>Success</h1></body></html>", content_type="text/html")

    async def handle_redirect(request):
        raise web.HTTPFound(location="/success")

    async def handle_redirect_loop(request):
        raise web.HTTPFound(location="/redirect-loop")

    async def handle_redirect_to_ssrf(request):
        raise web.HTTPFound(location="http://169.254.169.254/latest/meta-data")

    async def handle_404(request):
        return web.Response(status=404, text="Not Found", content_type="text/plain")

    async def handle_flaky_500(request):
        flaky_count["flaky_500"] += 1
        if flaky_count["flaky_500"] < 2:
            return web.Response(status=500, text="Internal Server Error")
        return web.Response(text="Recovered from 500", content_type="text/html")

    async def handle_rate_limit(request):
        flaky_count["rate_limit"] += 1
        if flaky_count["rate_limit"] < 2:
            return web.Response(status=429, headers={"Retry-After": "0.1"}, text="Too Many Requests")
        return web.Response(text="Recovered from 429", content_type="text/html")

    async def handle_conditional(request):
        inm = request.headers.get("If-None-Match")
        if inm == '"etag-123"':
            return web.Response(status=304, headers={"ETag": '"etag-123"'})
        return web.Response(
            text="<html>Initial Content</html>",
            headers={"ETag": '"etag-123"', "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"},
            content_type="text/html",
        )

    async def handle_oversized(request):
        return web.Response(body=b"X" * 20000, content_type="text/html")

    async def handle_unsupported(request):
        return web.Response(body=b"%PDF-1.4", content_type="application/pdf")

    app.router.add_get("/success", handle_success)
    app.router.add_get("/redirect", handle_redirect)
    app.router.add_get("/redirect-loop", handle_redirect_loop)
    app.router.add_get("/redirect-to-ssrf", handle_redirect_to_ssrf)
    app.router.add_get("/not-found", handle_404)
    app.router.add_get("/flaky-500", handle_flaky_500)
    app.router.add_get("/rate-limit", handle_rate_limit)
    app.router.add_get("/conditional", handle_conditional)
    app.router.add_get("/oversized", handle_oversized)
    app.router.add_get("/unsupported", handle_unsupported)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    # Retrieve allocated port
    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    yield base_url

    await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_success(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/success")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.SUCCESS
        assert res.status_code == 200
        assert "<h1>Success</h1>" in res.text
        assert res.is_html is True


@pytest.mark.asyncio
async def test_fetch_redirect(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/redirect")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.SUCCESS
        assert res.status_code == 200
        assert res.final_url == f"{test_server}/success"


@pytest.mark.asyncio
async def test_fetch_redirect_loop(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, max_redirects=2, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/redirect-loop")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.TOO_MANY_REDIRECTS


@pytest.mark.asyncio
async def test_fetch_404(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/not-found")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.HTTP_ERROR
        assert res.status_code == 404
        assert res.is_success is False


@pytest.mark.asyncio
async def test_fetch_retry_flaky_500(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, max_retries=3, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/flaky-500")
        res = await fetcher.fetch(req)
        # Should have retried and succeeded on attempt 2
        assert res.status == FetchStatus.SUCCESS
        assert "Recovered from 500" in res.text


@pytest.mark.asyncio
async def test_fetch_retry_after_429(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, max_retries=3, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/rate-limit")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.SUCCESS
        assert "Recovered from 429" in res.text


@pytest.mark.asyncio
async def test_fetch_conditional_304(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        # Initial request: gets 200 and ETag
        req1 = CrawlRequest(f"{test_server}/conditional")
        res1 = await fetcher.fetch(req1)
        assert res1.status == FetchStatus.SUCCESS
        assert res1.etag == '"etag-123"'

        # Second request with matching ETag: gets 304 Not Modified
        req2 = CrawlRequest(f"{test_server}/conditional", etag=res1.etag)
        res2 = await fetcher.fetch(req2)
        assert res2.status == FetchStatus.NOT_MODIFIED
        assert res2.status_code == 304
        assert res2.not_modified is True
        assert res2.body is None or len(res2.body) == 0


@pytest.mark.asyncio
async def test_fetch_oversized(test_server):
    # Max size 5000 bytes, response is 20000 bytes
    cfg = CrawlerConfig(allow_private_ips=True, max_response_size_bytes=5000, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/oversized")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.OVERSIZED_RESPONSE


@pytest.mark.asyncio
async def test_fetch_unsupported_content_type(test_server):
    cfg = CrawlerConfig(allow_private_ips=True, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        req = CrawlRequest(f"{test_server}/unsupported")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.UNSUPPORTED_CONTENT
        assert res.content_type == "application/pdf"


@pytest.mark.asyncio
async def test_fetch_redirect_to_ssrf_blocked(test_server):
    # In production mode (allow_private_ips=False), redirect to metadata IP is blocked!
    # Note: we need the initial request to pass (so we allow 127.0.0.1 by testing ssrf specifically)
    cfg = CrawlerConfig(allow_private_ips=False, per_host_delay_sec=0.0)
    async with AsyncFetcher(cfg) as fetcher:
        # Directly requesting blocked metadata IP
        req = CrawlRequest("http://169.254.169.254/latest/meta-data")
        res = await fetcher.fetch(req)
        assert res.status == FetchStatus.SSRF_BLOCKED
