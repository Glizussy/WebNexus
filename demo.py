"""Executable demonstration of Person A's Web Crawler Acquisition Module.

Spins up a controlled local web server simulating a real-world multi-page website
with robots.txt, subpages, keyword priorities, and structured contracts.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from aiohttp import web

from crawler.config import CrawlerConfig
from crawler.engine import CrawlerEngine
from crawler.models import CrawlEvent, FetchResult

# Ensure stdout handles UTF-8 on Windows
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo")


async def create_demo_server() -> tuple[web.AppRunner, str]:
    """Create a local HTTP server with rich crawling targets."""
    app = web.Application()

    async def handle_robots(request):
        return web.Response(
            text="""User-agent: *
Disallow: /admin/
Disallow: /restricted-research/
Crawl-delay: 0.1
Sitemap: /sitemap.xml
""",
            content_type="text/plain",
        )

    async def handle_sitemap(request):
        return web.Response(
            text="""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>/research/breakthrough-2026</loc></url>
</urlset>
""",
            content_type="application/xml",
        )

    async def handle_home(request):
        return web.Response(
            text="""<!DOCTYPE html>
<html>
<head>
    <title>Academic Research Portal</title>
</head>
<body>
    <h1>Academic Research Portal</h1>
    <p>Welcome to our repository. Browse key publications below:</p>
    <ul>
        <li><a href="/research/breakthrough-2026">2026 Breakthrough Research Paper</a></li>
        <li><a href="/news/latest-updates?utm_source=twitter&article_id=101">Latest News Updates</a></li>
        <li><a href="/restricted-research/secret">Classified Data (Should be robots blocked)</a></li>
        <li><a href="/conditional-page">Conditional Content (ETag demo)</a></li>
    </ul>
</body>
</html>
""",
            content_type="text/html",
        )

    async def handle_research(request):
        return web.Response(
            text="""<!DOCTYPE html>
<html>
<head><title>Research Paper</title></head>
<body>
    <h1>Autonomous Web Crawlers in Distributed Systems</h1>
    <p>Full research content here...</p>
</body>
</html>
""",
            content_type="text/html",
        )

    async def handle_news(request):
        return web.Response(
            text="""<!DOCTYPE html>
<html>
<head><title>News Update</title></head>
<body>
    <h1>Academic News</h1>
    <p>Crawling project acquisition module deployed successfully.</p>
</body>
</html>
""",
            content_type="text/html",
        )

    async def handle_conditional(request):
        inm = request.headers.get("If-None-Match")
        if inm == '"demo-etag-v1"':
            return web.Response(status=304, headers={"ETag": '"demo-etag-v1"'})
        return web.Response(
            text="<h1>Conditional Resource Content</h1>",
            headers={"ETag": '"demo-etag-v1"'},
            content_type="text/html",
        )

    async def handle_restricted(request):
        return web.Response(text="<h1>Secret Area</h1>", content_type="text/html")

    app.router.add_get("/robots.txt", handle_robots)
    app.router.add_get("/sitemap.xml", handle_sitemap)
    app.router.add_get("/", handle_home)
    app.router.add_get("/research/breakthrough-2026", handle_research)
    app.router.add_get("/news/latest-updates", handle_news)
    app.router.add_get("/conditional-page", handle_conditional)
    app.router.add_get("/restricted-research/secret", handle_restricted)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    port = site._server.sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    return runner, base_url


async def main() -> None:
    print("=" * 70)
    print(">> Starting Web Crawler Acquisition Module Demonstration (Person A)")
    print("=" * 70)

    # 1. Start the ephemeral mock server
    runner, base_url = await create_demo_server()
    print(f"[*] Controlled test server running at: {base_url}\n")

    # 2. Configure CrawlerEngine
    # Note: allow_private_ips=True is used exclusively for this local demo test server
    config = CrawlerConfig(
        max_concurrency=4,
        per_host_delay_sec=0.05,
        allow_private_ips=True,
        allowed_domains={"127.0.0.1"},
        max_depth=2,
        state_db_path=":memory:",  # In-memory for fast demo run
        keyword_weights={"research": 5.0, "news": 2.0},
    )

    engine = CrawlerEngine(config=config)

    # 3. Teammate Integration: Person C Event Listener (Dashboard / Metrics)
    def on_crawl_event(event: CrawlEvent) -> None:
        icon = {
            "url_discovered": "[DISCOVERY]   ",
            "fetch_start":    "[FETCH_START] ",
            "fetch_success":  "[SUCCESS]     ",
            "not_modified":   "[NOT_MODIFIED]",
            "robots_blocked": "[ROBOTS_BLOCK]",
            "failure":        "[FAILURE]     ",
        }.get(event.event_type.value, "[EVENT]       ")

        extra = f" (Status: {event.status_code})" if event.status_code else ""
        if event.latency_ms is not None:
            extra += f" Latency: {event.latency_ms:.1f}ms"
        print(f"  {icon} {event.url}{extra}")

    engine.add_event_listener(on_crawl_event)

    # 4. Teammate Integration: Person B Pipeline Handler (Content Extraction & Storage)
    pipeline_records: list[FetchResult] = []

    def on_pipeline_handoff(result: FetchResult) -> None:
        pipeline_records.append(result)
        body_len = len(result.body) if result.body else 0
        print(
            f"    -> [Person B Pipeline Handoff] Status={result.status.value.upper()} | "
            f"Code={result.status_code} | Bytes={body_len} | ContentType={result.content_type}"
        )

    engine.set_pipeline_handler(on_pipeline_handoff)

    # 5. Add Seed URL and Run
    print(f"[*] Scheduling seed URL: {base_url}/")
    await engine.add_seed_url(f"{base_url}/", priority=10.0)

    print("\n[*] Starting acquisition crawl loop...\n")
    await engine.run()

    # 6. Report Execution Summary
    print("\n" + "=" * 70)
    print("ACQUISITION DEMONSTRATION SUMMARY")
    print("=" * 70)
    print(f"Total Pages Crawled:     {engine.pages_crawled}")
    print(f"Total URLs Discovered:   {engine.urls_discovered}")
    print(f"Robots-Blocked Pages:    {engine.pages_robots_blocked}")
    print(f"Pipeline Handoffs:       {len(pipeline_records)}")

    # Clean up server
    await runner.cleanup()
    print("\n[OK] Demonstration completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())
