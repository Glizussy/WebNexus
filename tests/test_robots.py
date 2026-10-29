"""Unit tests for RFC 9309 robots.txt parsing, matching, and caching."""

import pytest
from crawler.config import CrawlerConfig
from crawler.robots import RobotsFile, RobotsManager


def test_robots_allow_disallow_precedence():
    content = """
    User-agent: *
    Disallow: /private/
    Allow: /private/public-page.html
    Disallow: /blocked
    Allow: /blocked
    """
    rf = RobotsFile(content)
    # Longest match wins: /private/public-page.html (len 25) vs /private/ (len 9) -> Allowed
    assert rf.is_allowed("/private/public-page.html", "AcademicCrawler/1.0") is True
    # Disallowed: /private/secret.html matches /private/
    assert rf.is_allowed("/private/secret.html", "AcademicCrawler/1.0") is False
    # Equal length: RFC 9309 dictates Allow wins
    assert rf.is_allowed("/blocked", "AcademicCrawler/1.0") is True


def test_robots_user_agent_specific_group():
    content = """
    User-agent: Googlebot
    Disallow: /google-only/

    User-agent: AcademicCrawler
    Disallow: /academic-disallowed/
    Allow: /

    User-agent: *
    Disallow: /
    """
    rf = RobotsFile(content)
    # AcademicCrawler matches its own specific group
    assert rf.is_allowed("/academic-disallowed/", "AcademicCrawler/1.0") is False
    assert rf.is_allowed("/other-path", "AcademicCrawler/1.0") is True

    # Unknown bot matches wildcard group
    assert rf.is_allowed("/other-path", "SomeRandomBot/1.0") is False


def test_robots_wildcards_and_dollar():
    content = """
    User-agent: *
    Disallow: /*.pdf$
    Disallow: /temp/*/data
    """
    rf = RobotsFile(content)
    assert rf.is_allowed("/document.pdf", "bot") is False
    assert rf.is_allowed("/document.pdf.html", "bot") is True  # Didn't end with .pdf
    assert rf.is_allowed("/temp/123/data", "bot") is False
    assert rf.is_allowed("/temp/123/other", "bot") is True


def test_robots_crawl_delay_and_sitemaps():
    content = """
    User-agent: *
    Crawl-delay: 2.5
    Disallow: /admin
    Sitemap: https://example.com/sitemap.xml
    Sitemap: https://example.com/sitemap-news.xml
    """
    rf = RobotsFile(content)
    assert rf.get_crawl_delay("bot") == 2.5
    assert len(rf.sitemaps) == 2
    assert "https://example.com/sitemap.xml" in rf.sitemaps


@pytest.mark.asyncio
async def test_robots_manager_caching_and_4xx_5xx():
    cfg = CrawlerConfig(robots_cache_ttl_sec=3600)
    manager = RobotsManager(cfg)

    # 404 Not Found means crawler can assume full access
    manager.set_error_status("https://example.com", 404)
    assert await manager.can_fetch("https://example.com/any/path") is True

    # 500 Server Error means temporary failure -> access is denied
    manager.set_error_status("https://failing-site.org", 500)
    assert await manager.can_fetch("https://failing-site.org/any/path") is False
