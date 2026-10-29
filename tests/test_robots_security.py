"""Regression tests for robots.txt redirect handling, loop detection, and strict streaming size limits."""

import asyncio
import pytest
from aiohttp import web
from crawler.config import CrawlerConfig
from crawler.fetcher import AsyncFetcher
from crawler.robots import RobotsManager


@pytest.fixture
async def robots_test_server():
    """Spin up an ephemeral in-process HTTP test server with specialized robots endpoints."""
    app = web.Application()

    # 1. Normal robots.txt
    async def handle_normal(request):
        return web.Response(
            text="User-agent: *\nDisallow: /private/\n",
            content_type="text/plain",
        )

    # 2. Valid redirect
    async def handle_redirect_init(request):
        raise web.HTTPFound(location="/robots-target.txt")

    async def handle_redirect_target(request):
        return web.Response(
            text="User-agent: *\nDisallow: /admin/\n",
            content_type="text/plain",
        )

    # 3. Redirect to private IP (SSRF)
    async def handle_redirect_to_private(request):
        raise web.HTTPFound(location="http://10.0.0.1/robots.txt")

    # 4. Redirect loop
    async def handle_loop_a(request):
        raise web.HTTPFound(location="/robots-loop-b")

    async def handle_loop_b(request):
        raise web.HTTPFound(location="/robots-loop-a")

    # 5. Excessive redirects
    async def handle_chain(request):
        step = int(request.query.get("step", "1"))
        if step < 10:
            raise web.HTTPFound(location=f"/robots-chain?step={step + 1}")
        return web.Response(text="User-agent: *\nDisallow: /\n", content_type="text/plain")

    # 6. Oversized robots.txt (e.g. 100 KB when limit is set to 20 KB)
    async def handle_oversized(request):
        # 100 KB of text
        big_content = "User-agent: *\n" + ("Disallow: /test/\n" * 6000)
        return web.Response(text=big_content, content_type="text/plain")

    # 7. Missing Content-Length header that exceeds limit while streaming
    async def handle_chunked_oversized(request):
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/plain"})
        await response.prepare(request)
        # Send chunks exceeding 25 KB
        for _ in range(30):
            await response.write(b"X" * 1024)
        await response.write_eof()
        return response

    # 8. Valid chunked response without Content-Length header (within size limit)
    async def handle_chunked_valid(request):
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/plain"})
        await response.prepare(request)
        await response.write(b"User-agent: *\nDisallow: /admin/\n")
        await response.write_eof()
        return response

    # 9. Oversized declared Content-Length header
    async def handle_declared_oversized(request):
        # Declares 10 MB in header
        return web.Response(
            status=200,
            headers={"Content-Length": "10485760"},
            text="User-agent: *\n",
            content_type="text/plain",
        )

    app.router.add_get("/robots-normal.txt", handle_normal)
    app.router.add_get("/robots-redirect.txt", handle_redirect_init)
    app.router.add_get("/robots-target.txt", handle_redirect_target)
    app.router.add_get("/robots-to-private.txt", handle_redirect_to_private)
    app.router.add_get("/robots-loop-a", handle_loop_a)
    app.router.add_get("/robots-loop-b", handle_loop_b)
    app.router.add_get("/robots-chain", handle_chain)
    app.router.add_get("/robots-oversized.txt", handle_oversized)
    app.router.add_get("/robots-chunked-oversized.txt", handle_chunked_oversized)
    app.router.add_get("/robots-chunked-valid.txt", handle_chunked_valid)
    app.router.add_get("/robots-declared-oversized.txt", handle_declared_oversized)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    yield base_url

    await runner.cleanup()


@pytest.mark.asyncio
async def test_robots_fetch_normal(robots_test_server):
    cfg = CrawlerConfig(allow_private_ips=True)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-normal.txt")
        assert status == 200
        assert content is not None
        assert "Disallow: /private/" in content


@pytest.mark.asyncio
async def test_robots_fetch_valid_redirect(robots_test_server):
    cfg = CrawlerConfig(allow_private_ips=True)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-redirect.txt")
        assert status == 200
        assert content is not None
        assert "Disallow: /admin/" in content


@pytest.mark.asyncio
async def test_robots_fetch_redirect_to_private_blocked(robots_test_server):
    # In production mode (allow_private_ips=False), redirecting to 10.0.0.1 is blocked by SSRF
    cfg = CrawlerConfig(allow_private_ips=False)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-to-private.txt")
        # Blocked, returns failure status 599
        assert status == 599
        assert content is None


@pytest.mark.asyncio
async def test_robots_fetch_redirect_loop_detected(robots_test_server):
    cfg = CrawlerConfig(allow_private_ips=True)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-loop-a")
        assert status == 599
        assert content is None


@pytest.mark.asyncio
async def test_robots_fetch_excessive_redirects(robots_test_server):
    cfg = CrawlerConfig(allow_private_ips=True, max_redirects=3)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-chain")
        assert status == 599
        assert content is None


@pytest.mark.asyncio
async def test_robots_fetch_oversized_response(robots_test_server):
    # Limit to 20 KB (20480 bytes); response is ~100 KB
    cfg = CrawlerConfig(allow_private_ips=True, max_robots_size_bytes=20480)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-oversized.txt")
        # Must return 413 (Payload Too Large) and never return partial text
        assert status == 413
        assert content is None


@pytest.mark.asyncio
async def test_robots_fetch_chunked_oversized_without_content_length(robots_test_server):
    cfg = CrawlerConfig(allow_private_ips=True, max_robots_size_bytes=10240)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-chunked-oversized.txt")
        assert status == 413
        assert content is None


@pytest.mark.asyncio
async def test_robots_fetch_missing_content_length_within_limit(robots_test_server):
    # No Content-Length header present; reads chunks up to EOF and succeeds if within limit
    cfg = CrawlerConfig(allow_private_ips=True, max_robots_size_bytes=524288)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-chunked-valid.txt")
        assert status == 200
        assert content is not None
        assert "Disallow: /admin/" in content


@pytest.mark.asyncio
async def test_robots_fetch_declared_oversized_content_length(robots_test_server):
    # Header declares 10 MB; limit is 512 KB. Should reject immediately with 413 without reading body
    cfg = CrawlerConfig(allow_private_ips=True, max_robots_size_bytes=524288)
    async with AsyncFetcher(cfg) as fetcher:
        status, content = await fetcher.fetch_robots_txt(f"{robots_test_server}/robots-declared-oversized.txt")
        assert status == 413
        assert content is None


@pytest.mark.asyncio
async def test_robots_manager_disallows_on_oversized_robots_txt(robots_test_server):
    """Verify that when robots.txt is oversized (413), RobotsManager disallows crawling rather than assuming allow-all."""
    cfg = CrawlerConfig(allow_private_ips=True, max_robots_size_bytes=20480)
    manager = RobotsManager(cfg)

    # Calling can_fetch will retrieve /robots-oversized.txt -> gets 413 -> sets temporary failure
    async with AsyncFetcher(cfg) as fetcher:
        allowed = await manager.can_fetch(
            f"{robots_test_server}/some/path",
            fetcher_callable=lambda _: fetcher.fetch_robots_txt(f"{robots_test_server}/robots-oversized.txt"),
        )
        # MUST be False: cannot assume all is allowed when policy was oversized!
        assert allowed is False
