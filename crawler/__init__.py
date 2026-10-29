"""Web Crawler Acquisition Module (Person A).

Responsible for:
- URL discovery, canonicalization, and prioritization
- RFC 9309 robots.txt compliance
- Asynchronous HTTP fetching with pooling, retries, and jitter
- Per-host rate limiting and Retry-After honouring
- Incremental fetching (ETag/Last-Modified conditional requests)
- SSRF prevention and destination validation
- Clear contracts for Person B (FetchResult) and Person C (CrawlEvent)
"""

from crawler.config import CrawlerConfig
from crawler.engine import CrawlerEngine
from crawler.models import (
    CrawlEvent,
    CrawlEventType,
    CrawlRequest,
    FetchError,
    FetchResult,
    FetchStatus,
)

__version__ = "0.1.0"
__all__ = [
    "CrawlerConfig",
    "CrawlerEngine",
    "CrawlRequest",
    "FetchResult",
    "FetchStatus",
    "FetchError",
    "CrawlEvent",
    "CrawlEventType",
]
