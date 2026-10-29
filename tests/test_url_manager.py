"""Unit tests for URL canonicalization, scope checking, and priority queue."""

import pytest
from crawler.config import CrawlerConfig
from crawler.models import CrawlRequest
from crawler.url_manager import (
    URLManager,
    canonicalize_url,
    compute_priority,
    is_url_in_scope,
    resolve_relative_url,
    sanitize_url_for_logging,
)


def test_canonicalize_url_strips_fragments():
    url = "https://example.com/page#section2"
    assert canonicalize_url(url) == "https://example.com/page"


def test_canonicalize_url_strips_tracking_params():
    url = "https://example.com/path?utm_source=twitter&article_id=42&fbclid=XYZ123&sort=asc"
    # article_id and sort are meaningful and must be preserved, sorted deterministically
    assert canonicalize_url(url) == "https://example.com/path?article_id=42&sort=asc"


def test_canonicalize_url_normalizes_ports_and_paths():
    url1 = "http://example.com:80/a/../b//c"
    assert canonicalize_url(url1) == "http://example.com/b/c"

    url2 = "https://example.com:443/docs/"
    assert canonicalize_url(url2) == "https://example.com/docs/"


def test_canonicalize_url_strips_userinfo():
    url = "https://admin:secret123@example.com/dashboard"
    assert canonicalize_url(url) == "https://example.com/dashboard"


def test_sanitize_url_for_logging():
    url = "https://user:password@example.com/api?token=secret_abc123&query=ai_papers&api_key=key_xyz"
    sanitized = sanitize_url_for_logging(url)
    assert "password" not in sanitized
    assert "secret_abc123" not in sanitized
    assert "key_xyz" not in sanitized
    assert "token=***" in sanitized
    assert "api_key=***" in sanitized
    assert "query=ai_papers" in sanitized


def test_resolve_relative_url():
    base = "https://example.com/blog/intro"
    assert resolve_relative_url(base, "article1") == "https://example.com/blog/article1"
    assert resolve_relative_url(base, "/about") == "https://example.com/about"
    assert resolve_relative_url(base, "https://other.org/feed") == "https://other.org/feed"
    assert resolve_relative_url(base, "javascript:void(0)") is None
    assert resolve_relative_url(base, "mailto:test@example.com") is None


def test_scope_filtering():
    allowed = {"example.com", ".university.edu"}
    assert is_url_in_scope("https://example.com/page", allowed, exact_subdomain_match=True) is True
    # Subdomain not allowed under exact match
    assert is_url_in_scope("https://sub.example.com/page", allowed, exact_subdomain_match=True) is False
    # Wildcard prefix allowed
    assert is_url_in_scope("https://cs.university.edu/papers", allowed, exact_subdomain_match=True) is True
    assert is_url_in_scope("https://malicious.com/page", allowed, exact_subdomain_match=True) is False


def test_priority_queue_order():
    cfg = CrawlerConfig(
        keyword_weights={"research": 5.0, "news": 2.0},
        source_weights={"high-impact.org": 10.0},
    )
    manager = URLManager(cfg)

    # Low priority
    manager.add_request(CrawlRequest("https://example.com/contact", depth=2))
    # High priority due to keyword "research" and lower depth
    manager.add_request(CrawlRequest("https://example.com/research-paper", depth=0))
    # Medium priority due to keyword "news"
    manager.add_request(CrawlRequest("https://example.com/news-item", depth=1))
    # Super high priority due to source weight
    manager.add_request(CrawlRequest("https://high-impact.org/frontpage", depth=0))

    # Expect pop order: high-impact.org -> research-paper -> news-item -> contact
    r1 = manager.pop_request()
    assert r1 is not None and "high-impact.org" in r1.url

    r2 = manager.pop_request()
    assert r2 is not None and "research-paper" in r2.url

    r3 = manager.pop_request()
    assert r3 is not None and "news-item" in r3.url

    r4 = manager.pop_request()
    assert r4 is not None and "contact" in r4.url

    assert manager.pop_request() is None


def test_duplicate_url_prevention():
    cfg = CrawlerConfig()
    manager = URLManager(cfg)

    req1 = CrawlRequest("https://example.com/page?a=1&b=2")
    req2 = CrawlRequest("https://example.com/page?b=2&a=1#section")

    assert manager.add_request(req1) is True
    # Second should be detected as duplicate and rejected
    assert manager.add_request(req2) is False
    assert len(manager) == 1
