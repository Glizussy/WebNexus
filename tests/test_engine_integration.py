"""Integration tests for CrawlerEngine using a controlled local website."""

import asyncio
import pytest
from aiohttp import web
from crawler.config import CrawlerConfig
from crawler.engine import CrawlerEngine
from crawler.models import CrawlEvent, CrawlEventType, FetchResult, FetchStatus


@pytest.fixture
async def controlled_crawl_site():
    """Spin up a controlled test website with multi-page structure and robots.txt."""
    app = web.Application()

    async def handle_robots(request):
        content = """User-agent: *
Disallow: /admin
Disallow: /secret-area
"""
        return web.Response(text=content, content_type="text/plain")

    async def handle_index(request):
        html = """<!DOCTYPE html>
        <html>
        <head><title>Test Home</title></head>
        <body>
            <h1>Welcome</h1>
            <a href="/research/paper-1">Research Paper 1</a>
            <a href="/news/update">News Update</a>
            <a href="/secret-area/confidential">Forbidden Secret</a>
            <a href="https://external-domain.org/skip">External Domain</a>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_research_paper(request):
        html = """<!DOCTYPE html>
        <html>
        <body>
            <h1>Research Paper 1</h1>
            <a href="/research/paper-2">Research Paper 2</a>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")

    async def handle_paper_2(request):
        return web.Response(text="<h1>Paper 2 Final</h1>", content_type="text/html")

    async def handle_news(request):
        return web.Response(text="<h1>News Update Details</h1>", content_type="text/html")

    async def handle_secret(request):
        return web.Response(text="<h1>Secret Data</h1>", content_type="text/html")

    app.router.add_get("/robots.txt", handle_robots)
    app.router.add_get("/", handle_index)
    app.router.add_get("/research/paper-1", handle_research_paper)
    app.router.add_get("/research/paper-2", handle_paper_2)
    app.router.add_get("/news/update", handle_news)
    app.router.add_get("/secret-area/confidential", handle_secret)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"

    yield base_url

    await runner.cleanup()


@pytest.mark.asyncio
async def test_full_crawl_integration_flow(controlled_crawl_site):
    cfg = CrawlerConfig(
        state_db_path=":memory:",
        allow_private_ips=True,
        max_depth=3,
        per_host_delay_sec=0.0,
        allowed_domains={"127.0.0.1"},
    )
    engine = CrawlerEngine(config=cfg)

    # 1. Person B Pipeline collector
    received_results: list[FetchResult] = []

    def pipeline_handler(result: FetchResult):
        received_results.append(result)

    engine.set_pipeline_handler(pipeline_handler)

    # 2. Person C Event collector
    received_events: list[CrawlEvent] = []

    def event_listener(event: CrawlEvent):
        received_events.append(event)

    engine.add_event_listener(event_listener)

    # 3. Add seed URL and run
    await engine.add_seed_url(f"{controlled_crawl_site}/")
    await engine.run()

    # 4. Verify Person B Contract & Outcomes
    urls_crawled = {r.requested_url for r in received_results}
    assert f"{controlled_crawl_site}/" in urls_crawled
    assert f"{controlled_crawl_site}/research/paper-1" in urls_crawled
    assert f"{controlled_crawl_site}/news/update" in urls_crawled
    assert f"{controlled_crawl_site}/research/paper-2" in urls_crawled

    # Verify Robots.txt block was caught and handed off to Person B
    secret_results = [r for r in received_results if "/secret-area" in r.requested_url]
    assert len(secret_results) == 1
    assert secret_results[0].status == FetchStatus.ROBOTS_BLOCKED
    assert secret_results[0].error is not None
    assert "robots" in secret_results[0].error.message.lower()

    # Verify External domain was filtered out
    assert not any("external-domain.org" in r.requested_url for r in received_results)

    # 5. Verify Person C Events
    event_types = {e.event_type for e in received_events}
    assert CrawlEventType.URL_DISCOVERED in event_types
    assert CrawlEventType.FETCH_START in event_types
    assert CrawlEventType.FETCH_SUCCESS in event_types
    assert CrawlEventType.ROBOTS_BLOCKED in event_types

    # 6. Verify Engine Metrics
    assert engine.pages_crawled >= 4
    assert engine.pages_robots_blocked == 1
    assert engine.urls_discovered >= 4

    # 7. Verify Structured Metrics Dictionary (Person C API)
    metrics = engine.get_metrics()
    assert metrics["pages_crawled"] >= 4
    assert metrics["pages_robots_blocked"] == 1
    assert metrics["pages_succeeded"] >= 3
    assert metrics["urls_discovered"] >= 4
    assert metrics["average_response_time_ms"] >= 0.0
    assert metrics["uptime_sec"] >= 0.0


@pytest.mark.asyncio
async def test_crawler_engine_streaming_generator(controlled_crawl_site):
    cfg = CrawlerConfig(
        state_db_path=":memory:",
        allow_private_ips=True,
        max_depth=1,
        per_host_delay_sec=0.0,
        allowed_domains={"127.0.0.1"},
    )
    engine = CrawlerEngine(config=cfg)
    await engine.add_seed_url(f"{controlled_crawl_site}/")

    streamed_results: list[FetchResult] = []
    async for result in engine.run_generator():
        streamed_results.append(result)

    assert len(streamed_results) >= 1
    assert streamed_results[0].requested_url == f"{controlled_crawl_site}/"
    assert streamed_results[0].is_success is True


@pytest.mark.asyncio
async def test_crawler_engine_pause_resume():
    cfg = CrawlerConfig(state_db_path=":memory:", allow_private_ips=True)
    engine = CrawlerEngine(config=cfg)

    assert engine._paused is False
    engine.pause()
    assert engine._paused is True
    engine.resume()
    assert engine._paused is False
