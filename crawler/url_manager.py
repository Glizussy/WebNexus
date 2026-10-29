"""URL canonicalization, duplicate prevention, scope filtering, and stable priority queue."""

from __future__ import annotations

import heapq
import itertools
import posixpath
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from crawler.config import CrawlerConfig
from crawler.models import CrawlRequest

# Tracking query parameters commonly added for analytics/advertising that don't alter page content
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_source_platform",
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "msclkid",
        "zanpid",
        "mc_cid",
        "mc_eid",
        "igshid",
        "_hsenc",
        "_hsmi",
        "yclid",
        "gbraid",
        "wbraid",
        "ttclid",
        "twclid",
        "_openstat",
    }
)

# Common sensitive parameter names to mask in logs
SENSITIVE_PARAM_NAMES = frozenset(
    {
        "token",
        "access_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "passwd",
        "auth",
        "authorization",
        "session",
        "session_id",
        "client_secret",
    }
)


def sanitize_url_for_logging(url: str) -> str:
    """Mask credentials and sensitive query parameters (e.g., token, secret, password) in URLs for safe logging."""
    if not url:
        return ""
    try:
        split = urlsplit(url)
        # Mask user:pass in authority if present
        netloc = split.netloc
        if "@" in netloc:
            userinfo, host = netloc.split("@", 1)
            netloc = f"***:***@{host}"

        if not split.query:
            return urlunsplit((split.scheme, netloc, split.path, "", ""))

        masked_query: list[tuple[str, str]] = []
        for k, v in parse_qsl(split.query, keep_blank_values=True):
            if k.lower() in SENSITIVE_PARAM_NAMES:
                masked_query.append((k, "***"))
            else:
                masked_query.append((k, v))

        return urlunsplit((split.scheme, netloc, split.path, urlencode(masked_query, safe="*"), ""))
    except Exception:
        return "[UNPARSEABLE_URL]"


def canonicalize_url(url: str, strip_tracking: bool = True) -> str:
    """Canonicalize a URL to prevent duplicate crawling of identical endpoints.

    - Trims whitespace.
    - Lowers scheme and host.
    - Strips standard default ports (:80 for http, :443 for https).
    - Normalizes path resolving dots/slashes.
    - Strips URL fragments (#section).
    - Strips known marketing/tracking query parameters (utm_*, fbclid, etc.).
    - Sorts remaining meaningful query parameters deterministically.
    """
    if not url or not url.strip():
        raise ValueError("Cannot canonicalize an empty URL")

    url = url.strip()
    split = urlsplit(url)

    scheme = split.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"Unsupported scheme '{scheme}'")

    netloc = split.netloc.lower()
    if not netloc:
        raise ValueError(f"URL missing network location (host): {url}")

    # Strip embedded user credentials (user:pass@host) to prevent leaks and duplication
    if "@" in netloc:
        netloc = netloc.split("@", 1)[1]

    # Remove standard default ports
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    # Normalize path
    raw_path = split.path or "/"
    # Collapse multiple slashes
    raw_path = re.sub(r"/+", "/", raw_path)

    # Preserve trailing slash intent if original path ended with '/' and isn't just '/'
    ends_with_slash = raw_path.endswith("/") and raw_path != "/"

    normalized_path = posixpath.normpath(raw_path)
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    if ends_with_slash and not normalized_path.endswith("/"):
        normalized_path += "/"

    # Query normalization
    query_parts: list[tuple[str, str]] = []
    if split.query:
        for k, v in parse_qsl(split.query, keep_blank_values=True):
            k_lower = k.lower()
            if strip_tracking and k_lower in TRACKING_PARAMS:
                continue
            query_parts.append((k, v))
        # Sort query params deterministically by key, then value
        query_parts.sort(key=lambda item: (item[0], item[1]))

    normalized_query = urlencode(query_parts, doseq=True)

    # Note: fragments are explicitly stripped as per standard web crawler canonicalization
    return urlunsplit((scheme, netloc, normalized_path, normalized_query, ""))


def resolve_relative_url(base_url: str, link: str) -> str | None:
    """Resolve a relative URL against a base URL and validate scheme."""
    try:
        joined = urljoin(base_url, link.strip())
        split = urlsplit(joined)
        if split.scheme.lower() not in ("http", "https"):
            return None
        if not split.netloc:
            return None
        return canonicalize_url(joined)
    except Exception:
        return None


def is_url_in_scope(url: str, allowed_domains: set[str], exact_subdomain_match: bool = True) -> bool:
    """Check if a URL falls within allowed domain scopes.

    - If allowed_domains is empty, all domains are considered in scope.
    - If exact_subdomain_match is True, 'sub.example.com' will not match 'example.com'
      unless explicitly listed or prefixed with a wildcard like '.example.com'.
    """
    if not allowed_domains:
        return True

    try:
        split = urlsplit(url)
        host = split.hostname
        if not host:
            return False
        host = host.lower()
    except Exception:
        return False

    for allowed in allowed_domains:
        allowed = allowed.lower().strip()
        if allowed.startswith("."):
            # Wildcard domain suffix match
            domain_suffix = allowed[1:]
            if host == domain_suffix or host.endswith(allowed):
                return True
        elif exact_subdomain_match:
            if host == allowed:
                return True
        else:
            if host == allowed or host.endswith("." + allowed):
                return True

    return False


def compute_priority(
    url: str,
    depth: int,
    base_priority: float = 0.0,
    keyword_weights: dict[str, float] | None = None,
    source_weights: dict[str, float] | None = None,
    source_id: str | None = None,
    depth_penalty: float = 0.5,
) -> float:
    """Calculate the priority score for a URL.

    Higher scores indicate higher crawling priority.
    """
    priority = base_priority
    url_lower = url.lower()

    if keyword_weights:
        for kw, weight in keyword_weights.items():
            if kw.lower() in url_lower:
                priority += weight

    if source_weights and source_id and source_id in source_weights:
        priority += source_weights[source_id]
    elif source_weights:
        try:
            host = urlsplit(url).hostname
            if host and host in source_weights:
                priority += source_weights[host]
        except Exception:
            pass

    # Depth penalty: shallower pages are prioritized over deeper links
    priority -= depth * depth_penalty
    return priority


class URLManager:
    """Manages URL discovery, deduplication, scope filtering, and a stable priority queue."""

    def __init__(self, config: CrawlerConfig) -> None:
        self.config = config
        self._seen_urls: set[str] = set()
        # Min-heap storing (-priority, sequence_id, CrawlRequest) for stable max-priority ordering
        self._queue: list[tuple[float, int, CrawlRequest]] = []
        self._counter = itertools.count()
        self._total_scheduled = 0

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def seen_count(self) -> int:
        return len(self._seen_urls)

    @property
    def total_scheduled(self) -> int:
        return self._total_scheduled

    def is_seen(self, url: str) -> bool:
        """Check if canonical form of URL has already been scheduled or crawled."""
        try:
            canonical = canonicalize_url(url)
            return canonical in self._seen_urls
        except Exception:
            return False

    def mark_seen(self, canonical_url: str) -> None:
        """Explicitly record a canonical URL as seen (e.g., loaded from persistent state)."""
        self._seen_urls.add(canonical_url)

    def add_request(self, request: CrawlRequest, force: bool = False) -> bool:
        """Add a crawl request to the priority queue if valid, within scope, and not previously seen.

        Returns True if the URL was successfully scheduled, False otherwise.
        """
        # Validate depth
        if request.depth > self.config.max_depth:
            return False

        # Validate scheme and canonicalize
        try:
            canonical = canonicalize_url(request.url)
        except Exception:
            return False

        # Check deduplication
        if not force and canonical in self._seen_urls:
            return False

        # Check domain scope
        allowed = request.allowed_domains or self.config.allowed_domains
        if not is_url_in_scope(canonical, allowed):
            return False

        # Update request URL to canonical form
        request.url = canonical

        # If priority not explicitly overridden, compute using configured weights
        if request.priority == 0.0:
            request.priority = compute_priority(
                url=canonical,
                depth=request.depth,
                base_priority=0.0,
                keyword_weights=self.config.keyword_weights,
                source_weights=self.config.source_weights,
                source_id=request.source_id,
            )

        self._seen_urls.add(canonical)
        seq = next(self._counter)
        # Store negative priority so heap acts as a max-priority queue
        heapq.heappush(self._queue, (-request.priority, seq, request))
        self._total_scheduled += 1
        return True

    def pop_request(self) -> CrawlRequest | None:
        """Pop the highest priority CrawlRequest from the queue."""
        if not self._queue:
            return None
        _, _, request = heapq.heappop(self._queue)
        return request

    def peek_priority(self) -> float | None:
        """Peek at the highest priority value without removing the request."""
        if not self._queue:
            return None
        neg_prio, _, _ = self._queue[0]
        return -neg_prio

    def clear(self) -> None:
        """Clear queue and seen set."""
        self._queue.clear()
        self._seen_urls.clear()
