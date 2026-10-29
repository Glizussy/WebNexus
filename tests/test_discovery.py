"""Unit tests for URL discovery from HTML, RSS, Atom, and XML Sitemaps."""

import pytest
from crawler.config import CrawlerConfig
from crawler.discovery import (
    URLDiscoveryService,
    discover_feed_links_in_html,
    extract_atom_links,
    extract_html_links,
    extract_rss_links,
    parse_xml_sitemap,
)
from crawler.models import FetchResult, FetchStatus


def test_extract_html_links():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <base href="https://example.com/blog/">
        <link rel="alternate" type="application/rss+xml" href="/feed.xml">
    </head>
    <body>
        <a href="article-1.html">Article 1</a>
        <a href="../about">About Us</a>
        <a href="https://other.org/external">External</a>
        <a href="javascript:alert(1)">Bad Link</a>
        <a href="mailto:test@example.com">Email</a>
        <a href="#section2">Hash</a>
        <iframe src="widget.html"></iframe>
    </body>
    </html>
    """
    links = list(extract_html_links(html, "https://example.com/blog/"))
    assert "https://example.com/blog/article-1.html" in links
    assert "https://example.com/about" in links
    assert "https://other.org/external" in links
    assert "https://example.com/blog/widget.html" in links
    # Should not include javascript, mailto, or pure hash
    assert not any("javascript" in link for link in links)
    assert not any("mailto" in link for link in links)


def test_discover_feed_links_in_html():
    html = """
    <html>
    <head>
        <link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.rss" />
        <link rel="alternate" type="application/atom+xml" title="Atom" href="https://example.com/atom.xml" />
    </head>
    <body></body>
    </html>
    """
    feeds = discover_feed_links_in_html(html, "https://example.com/")
    assert "https://example.com/feed.rss" in feeds
    assert "https://example.com/atom.xml" in feeds


def test_extract_rss_links():
    rss_xml = """<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0">
    <channel>
        <title>Tech News</title>
        <item>
            <title>New AI Paper</title>
            <link>https://example.com/news/ai-paper</link>
        </item>
        <item>
            <title>Crawler Launch</title>
            <link>/news/crawler-launch</link>
        </item>
    </channel>
    </rss>
    """
    urls = extract_rss_links(rss_xml, "https://example.com")
    assert "https://example.com/news/ai-paper" in urls
    assert "https://example.com/news/crawler-launch" in urls


def test_extract_atom_links():
    atom_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <title>Example Feed</title>
        <entry>
            <title>Atom Entry 1</title>
            <link rel="alternate" href="https://example.com/entries/1"/>
        </entry>
        <entry>
            <title>Atom Entry 2</title>
            <link href="/entries/2"/>
        </entry>
    </feed>
    """
    urls = extract_atom_links(atom_xml, "https://example.com")
    assert "https://example.com/entries/1" in urls
    assert "https://example.com/entries/2" in urls


def test_parse_xml_sitemap_and_index():
    sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/page1</loc></url>
        <url><loc>https://example.com/page2</loc></url>
    </urlset>
    """
    pages, indices = parse_xml_sitemap(sitemap_xml, "https://example.com")
    assert len(pages) == 2
    assert "https://example.com/page1" in pages
    assert len(indices) == 0

    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
        <sitemap><loc>/sitemap-2.xml</loc></sitemap>
    </sitemapindex>
    """
    pages2, indices2 = parse_xml_sitemap(sitemap_index_xml, "https://example.com")
    assert len(pages2) == 0
    assert len(indices2) == 2
    assert "https://example.com/sitemap-2.xml" in indices2


def test_url_discovery_service():
    cfg = CrawlerConfig(max_depth=2, allowed_domains={"example.com"})
    svc = URLDiscoveryService(cfg)

    html = '<a href="/page1">1</a><a href="https://external.org/test">Ext</a>'
    res = FetchResult(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status=FetchStatus.SUCCESS,
        status_code=200,
        text=html,
        content_type="text/html",
        depth=0,
    )

    requests = svc.discover_from_result(res, current_depth=0)
    # external.org is not in allowed_domains, so only page1 should be discovered
    assert len(requests) == 1
    assert requests[0].url == "https://example.com/page1"
    assert requests[0].depth == 1
