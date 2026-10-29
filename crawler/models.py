"""Domain models and shared contracts for Person A (Acquisition), Person B (Pipeline), and Person C (API/Dashboard)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class FetchStatus(str, Enum):
    """Normalized status of an attempted crawl request."""
    SUCCESS = "success"
    NOT_MODIFIED = "not_modified"  # HTTP 304
    HTTP_ERROR = "http_error"      # HTTP 4xx, 5xx (non-transient or exhausted retries)
    NETWORK_ERROR = "network_error" # DNS, ConnectionReset, SSL, etc.
    TIMEOUT = "timeout"            # Connect or read timeout
    ROBOTS_BLOCKED = "robots_blocked" # Disallowed by RFC 9309 robots.txt
    SSRF_BLOCKED = "ssrf_blocked"  # Prohibited private/loopback/metadata IP
    UNSUPPORTED_CONTENT = "unsupported_content" # Non-textual, disallowed mime type
    TOO_MANY_REDIRECTS = "too_many_redirects"
    OVERSIZED_RESPONSE = "oversized_response"
    CANCELLED = "cancelled"


class CrawlEventType(str, Enum):
    """Event types emitted to Person C's dashboard/metrics system."""
    URL_DISCOVERED = "url_discovered"
    FETCH_START = "fetch_start"
    FETCH_SUCCESS = "fetch_success"
    NOT_MODIFIED = "not_modified"
    RETRY = "retry"
    FAILURE = "failure"
    ROBOTS_BLOCKED = "robots_blocked"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True)
class FetchError:
    """Structured error information."""
    error_type: str
    message: str
    status_code: int | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "message": self.message,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }


@dataclass
class CrawlRequest:
    """Acquisition request representing a URL to fetch with scheduling metadata."""
    url: str
    depth: int = 0
    priority: float = 0.0
    source_id: str | None = None
    allowed_domains: set[str] = field(default_factory=set)
    etag: str | None = None
    last_modified: str | None = None
    retry_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    scheduled_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise ValueError("depth cannot be negative")


@dataclass
class FetchResult:
    """Shared Contract with Person B (Pipeline / Content Extraction).

    Represents the full observable outcome of an attempted URL fetch.
    """
    requested_url: str
    final_url: str
    status: FetchStatus
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    text: str | None = None
    content_type: str = ""
    encoding: str = "utf-8"
    fetch_timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: FetchError | None = None
    retry_count: int = 0
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    latency_ms: float = 0.0
    depth: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_success(self) -> bool:
        """True if the page was fetched successfully (200-299) or confirmed not modified (304)."""
        return self.status in (FetchStatus.SUCCESS, FetchStatus.NOT_MODIFIED)

    @property
    def is_html(self) -> bool:
        """True if content type indicates HTML."""
        ct = self.content_type.lower()
        return "text/html" in ct or "application/xhtml+xml" in ct

    @property
    def is_xml(self) -> bool:
        """True if content type indicates XML/RSS/Atom."""
        ct = self.content_type.lower()
        return "xml" in ct or "atom" in ct or "rss" in ct

    def to_dict(self, include_body: bool = False) -> dict[str, Any]:
        """Convert result to dictionary for logging or API responses (Person C)."""
        data: dict[str, Any] = {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status": self.status.value,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "fetch_timestamp_utc": self.fetch_timestamp_utc.isoformat(),
            "retry_count": self.retry_count,
            "not_modified": self.not_modified,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "latency_ms": round(self.latency_ms, 2),
            "depth": self.depth,
            "error": self.error.to_dict() if self.error else None,
            "metadata": self.metadata,
        }
        if include_body:
            data["body_length"] = len(self.body) if self.body else 0
            data["text_length"] = len(self.text) if self.text else 0
        return data


@dataclass(frozen=True)
class CrawlEvent:
    """Shared Contract with Person C (Dashboard / Metrics / Job Orchestration)."""
    event_type: CrawlEventType
    url: str
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status_code: int | None = None
    latency_ms: float | None = None
    retry_count: int = 0
    message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "url": self.url,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "status_code": self.status_code,
            "latency_ms": round(self.latency_ms, 2) if self.latency_ms is not None else None,
            "retry_count": self.retry_count,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class URLRecord:
    """Persistent crawl state entry for a URL across engine runs."""
    url: str
    first_seen_at: datetime
    last_crawled_at: datetime | None = None
    status: FetchStatus = FetchStatus.SUCCESS
    status_code: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    next_fetch_at: datetime | None = None
    consecutive_failures: int = 0
    crawl_count: int = 0
