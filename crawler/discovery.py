"""URL discovery from HTML documents, RSS/Atom feeds, and XML sitemaps/sitemap indexes."""

from __future__ import annotations

import logging
import re
from typing import Iterator
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

import lxml.html

from crawler.config import CrawlerConfig
from crawler.models import CrawlRequest, FetchResult
from crawler.url_manager import canonicalize_url, is_url_in_scope, resolve_relative_url

logger = logging.getLogger(__name__)


def extract_html_links(
    html_content: str,
    base_url: str,
) -> Iterator[str]:
    """Extract valid, canonical HTTP/HTTPS links from HTML content."""
    if not html_content or not html_content.strip():
        return

    try:
        doc = lxml.html.fromstring(html_content)
    except Exception as e:
        logger.debug(f"Failed to parse HTML for link discovery at {base_url}: {e}")
        return

    # Check for <base href="...">
    base_elements = doc.xpath("//base[@href]")
    effective_base = base_url
    if base_elements:
        href = base_elements[0].get("href")
        if href:
            effective_base = urljoin(base_url, href.strip())

    # 1. <a> links
    for a in doc.xpath("//a[@href]"):
        raw_href = a.get("href")
        if raw_href:
            resolved = resolve_relative_url(effective_base, raw_href)
            if resolved:
                yield resolved

    # 2. <link> tags in <head> (e.g. canonical, alternates)
    for link_el in doc.xpath("//link[@href]"):
        rel = (link_el.get("rel") or "").lower()
        if rel in ("alternate", "next", "prev", "canonical"):
            raw_href = link_el.get("href")
            if raw_href:
                resolved = resolve_relative_url(effective_base, raw_href)
                if resolved:
                    yield resolved

    # 3. <iframe> src
    for iframe in doc.xpath("//iframe[@src]"):
        src = iframe.get("src")
        if src:
            resolved = resolve_relative_url(effective_base, src)
            if resolved:
                yield resolved


def discover_feed_links_in_html(html_content: str, base_url: str) -> list[str]:
    """Discover RSS/Atom feed URLs declared in HTML <head>."""
    if not html_content:
        return []
    feeds: list[str] = []
    try:
        doc = lxml.html.fromstring(html_content)
        for link in doc.xpath("//link[@rel='alternate']"):
            type_attr = (link.get("type") or "").lower()
            if "rss" in type_attr or "atom" in type_attr:
                href = link.get("href")
                if href:
                    resolved = resolve_relative_url(base_url, href)
                    if resolved:
                        feeds.append(resolved)
    except Exception:
        pass
    return feeds


def extract_rss_links(content: str | bytes, base_url: str) -> list[str]:
    """Extract item URLs from RSS 2.0 / RSS 1.0 feeds."""
    urls: list[str] = []
    if isinstance(content, str):
        content_bytes = content.encode("utf-8", errors="replace")
    else:
        content_bytes = content

    try:
        root = ET.fromstring(content_bytes)
        # Handle standard RSS 2.0 <channel><item><link>
        for item in root.findall(".//item"):
            link_el = item.find("link")
            if link_el is not None and link_el.text:
                resolved = resolve_relative_url(base_url, link_el.text)
                if resolved:
                    urls.append(resolved)
    except Exception as e:
        logger.debug(f"Failed to parse RSS feed from {base_url}: {e}")
    return urls


def extract_atom_links(content: str | bytes, base_url: str) -> list[str]:
    """Extract entry URLs from Atom feeds (<feed><entry><link href="...">)."""
    urls: list[str] = []
    if isinstance(content, str):
        content_bytes = content.encode("utf-8", errors="replace")
    else:
        content_bytes = content

    try:
        root = ET.fromstring(content_bytes)
        # Handle Atom namespace (xmlns="http://www.w3.org/2005/Atom") or non-namespaced
        # Use local-name tag stripping
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "entry":
                # Find link children
                for child in elem:
                    child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if child_tag == "link":
                        href = child.get("href")
                        rel = child.get("rel") or "alternate"
                        if href and rel in ("alternate", ""):
                            resolved = resolve_relative_url(base_url, href)
                            if resolved:
                                urls.append(resolved)
                                break
    except Exception as e:
        logger.debug(f"Failed to parse Atom feed from {base_url}: {e}")
    return urls


def parse_xml_sitemap(
    content: str | bytes,
    base_url: str,
) -> tuple[list[str], list[str]]:
    """Parse an XML sitemap or sitemap index.

    Returns:
        (page_urls, sitemap_index_urls)
    """
    page_urls: list[str] = []
    sitemap_index_urls: list[str] = []

    if isinstance(content, str):
        content_bytes = content.encode("utf-8", errors="replace")
    else:
        content_bytes = content

    try:
        root = ET.fromstring(content_bytes)
        root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        if root_tag == "sitemapindex":
            # Sitemap index pointing to other sitemaps: <sitemap><loc>...</loc></sitemap>
            for loc in root.findall(".//{*}loc"):
                if loc.text and loc.text.strip():
                    resolved = resolve_relative_url(base_url, loc.text.strip())
                    if resolved:
                        sitemap_index_urls.append(resolved)
        elif root_tag == "urlset":
            # Regular sitemap: <url><loc>...</loc></url>
            for loc in root.findall(".//{*}loc"):
                if loc.text and loc.text.strip():
                    resolved = resolve_relative_url(base_url, loc.text.strip())
                    if resolved:
                        page_urls.append(resolved)
    except Exception as e:
        logger.debug(f"Failed to parse XML sitemap from {base_url}: {e}")

    return page_urls, sitemap_index_urls


class URLDiscoveryService:
    """Discovers new URLs from fetched pages (HTML, RSS/Atom, Sitemaps) and emits CrawlRequests."""

    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config

    def discover_from_result(
        self,
        result: FetchResult,
        current_depth: int,
        allowed_domains: set[str] | None = None,
    ) -> list[CrawlRequest]:
        """Discover new crawl requests from a successful fetch result."""
        if not result.is_success or not result.text:
            return []

        next_depth = current_depth + 1
        if next_depth > self.config.max_depth:
            return []

        domains = allowed_domains or self.config.allowed_domains
        discovered_urls: set[str] = set()

        if result.is_html:
            for url in extract_html_links(result.text, result.final_url):
                if is_url_in_scope(url, domains):
                    discovered_urls.add(url)
        elif result.is_xml:
            # Check for sitemap
            pages, sitemaps = parse_xml_sitemap(result.text, result.final_url)
            for url in pages:
                if is_url_in_scope(url, domains):
                    discovered_urls.add(url)
            for url in sitemaps:
                if is_url_in_scope(url, domains):
                    discovered_urls.add(url)

            # Check for RSS / Atom
            rss_links = extract_rss_links(result.text, result.final_url)
            for url in rss_links:
                if is_url_in_scope(url, domains):
                    discovered_urls.add(url)

            atom_links = extract_atom_links(result.text, result.final_url)
            for url in atom_links:
                if is_url_in_scope(url, domains):
                    discovered_urls.add(url)

        # Convert discovered URLs to CrawlRequest items
        requests: list[CrawlRequest] = []
        for url in discovered_urls:
            requests.append(
                CrawlRequest(
                    url=url,
                    depth=next_depth,
                    allowed_domains=domains,
                    metadata={"parent_url": result.final_url},
                )
            )

        return requests
