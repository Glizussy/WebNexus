# Web Crawler Acquisition Module (Person A)

An asynchronous, resilient, RFC 9309-compliant web crawler acquisition module implemented in Python 3.12 (`aiohttp`).

Designed for an academic research project with three defined engineering roles:
- **Person A (`crawler/`)**: URL discovery, canonicalization, prioritization, RFC 9309 robots.txt compliance, asynchronous HTTP fetching, retries with jitter, per-host rate limiting, SSRF defense, incremental scheduling (ETag / Last-Modified), and state persistence.
- **Person B (`pipeline/`, `storage/`)**: Content extraction, normalization, keyword relevance, duplicate detection, and PostgreSQL storage.
- **Person C (`api/`, `web/`, `infra/`)**: Job orchestration, REST APIs, dashboard, metrics, Docker, and CI/CD.

---

## 1. Module Architecture

```
DWM/
├── crawler/
│   ├── __init__.py             # Public exports (CrawlerEngine, CrawlerConfig, models)
│   ├── config.py               # Validated configuration and environment variable loading
│   ├── models.py               # Shared contracts (FetchResult, CrawlEvent, CrawlRequest)
│   ├── ssrf.py                 # SSRF protection, IP/DNS validation, and metadata IP defense
│   ├── url_manager.py          # Canonicalization, deduplication, scope filtering, priority queue
│   ├── robots.py               # RFC 9309 robots.txt parser, evaluator, and origin cache
│   ├── rate_limiter.py         # Per-host rate limiting, Retry-After parsing/capping (HTTP-date & sec)
│   ├── fetcher.py              # Asynchronous HTTP fetcher with pooling, backoff, jitter, streaming
│   ├── discovery.py            # Link extraction from HTML, RSS 2.0, Atom feeds, and XML sitemaps
│   ├── state.py                # Persistent state store (SQLite) & In-Memory testing store
│   ├── scheduler.py            # Incremental recrawl scheduler and conditional HTTP validators
│   └── engine.py               # Master crawler engine coordinating workers and teammate contracts
├── tests/                      # Deterministic, comprehensive pytest suite (69 tests)
│   ├── test_config.py
│   ├── test_ssrf.py
│   ├── test_ssrf_regression.py # Connection-time DNS rebinding & multi-answer tests
│   ├── test_url_manager.py
│   ├── test_robots.py
│   ├── test_robots_security.py # Streaming limits, redirect loops & SSRF redirect tests
│   ├── test_rate_limiter.py
│   ├── test_fetcher.py
│   ├── test_discovery.py
│   ├── test_scheduler.py
│   └── test_engine_integration.py
├── demo.py                     # Executable demonstration with local HTTP mock server
├── pyproject.toml              # Dependencies and pytest configuration
├── .env.example                # Configuration template
└── .github/workflows/ci.yml    # CI pipeline for Python 3.11 & 3.12
```

---

## 2. Key Features

- **Asynchronous HTTP Fetching (`crawler/fetcher.py`)**:
  - Connection pooling via `aiohttp.TCPConnector` with connection limits per host.
  - Bounded concurrency with `asyncio.Semaphore`.
  - Configurable connection and request timeouts (`sock_connect`, `total`).
  - Bounded retries with exponential backoff and full jitter.
  - Capped `Retry-After` parsing supporting both integer seconds and RFC 7231 / RFC 9110 HTTP-dates.
  - Streaming body reads with maximum size cut-off (`max_response_size_bytes`) to prevent decompression bombs.
  - Case-insensitive header lookup and clean redirect following with per-hop destination validation.

- **SSRF & Security Defense (`crawler/ssrf.py`)**:
  - Blocks private IPv4/IPv6 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), loopback (`127.0.0.1`, `::1`), link-local, multicast, reserved/unspecified, IPv4-mapped IPv6, and cloud metadata services (`169.254.169.254`, `fd00:ec2::254`).
  - **Connection-Time DNS Rebinding Defense**: Implements `SafeSSRFResolver` registered with `aiohttp.TCPConnector(resolver=...)`. Validates all resolved IP addresses at socket creation time, preventing TOCTOU DNS rebinding attacks while preserving TLS SNI and hostname certificate validation.
  - **Fail-Closed Multi-Answer Resolution**: If a hostname resolves to multiple IP addresses (e.g. mixed public and private answers), the resolver fails closed and denies the connection.
  - Revalidates destination IP and hostname on every redirect hop for both page fetches and `robots.txt`.

- **RFC 9309 Robots.txt Compliance (`crawler/robots.py`, `crawler/fetcher.py`)**:
  - User-agent token matching (most specific product token, with fallback to wildcard `*`).
  - Precedence matching: longest matching rule path wins; if rule lengths are equal, `Allow` takes precedence over `Disallow`.
  - Handles wildcards (`*`) and end-anchors (`$`).
  - Origin-level policy caching with configurable TTL (default 24h).
  - Proper RFC 9309 response handling: 4xx allows all paths, 5xx treats as temporary unavailability (access denied during failure).
  - Respects `Crawl-delay` and discovers `Sitemap:` URLs.
  - **Redirect Loop & SSRF Defense for Robots**: Manual per-hop redirect loop detection (`redirect_history`) and per-hop destination validation (`validate_destination_async`).
  - **Strict Streaming Size Limit (`max_robots_size_bytes`)**: Streams response body chunk-by-chunk up to configured limit (default 512 KiB) to defeat decompression bombs. Aborts with HTTP 413 if exceeded, handling missing or misleading `Content-Length` headers safely.
  - **Conservative Error Semantics**: Treats HTTP 413 (Payload Too Large) as temporary failure (crawl disallowed) rather than assuming full access.

- **URL Discovery & Prioritization (`crawler/url_manager.py`, `crawler/discovery.py`)**:
  - Canonicalization: removes URL fragments (`#...`), strips tracking parameters (`utm_*`, `fbclid`, `gclid`, etc.), sorts query arguments, and normalizes paths.
  - Deduplication: in-memory set and persistent database tracking prevents duplicate fetches.
  - Priority queue: stable max-heap priority queue weighting keyword relevance (e.g. `research`, `paper`, `news`), source authority, and depth penalty.
  - Multi-source discovery: extracts URLs from HTML (`<a>`, `<link>`, `<iframe>`), RSS 2.0 feeds, Atom feeds, XML sitemaps (`<urlset>`), and sitemap indexes (`<sitemapindex>`).

- **Incremental Fetching (`crawler/scheduler.py`, `crawler/state.py`)**:
  - Conditional HTTP requests: saves `ETag` and `Last-Modified` headers.
  - Sends `If-None-Match` and `If-Modified-Since`.
  - Handles `304 Not Modified` without body parsing, preserving existing content hashes and validators.
  - Recrawl scheduling: persists `next_fetch_at` timestamps in SQLite (`crawl_state.db`) so history and schedules survive application restarts.

---

## 3. Teammate Integration Contracts

### Contract with Person B: Downstream Pipeline & Storage
Person B can consume crawl results either via callback or async generator stream.

#### `FetchResult` Model
```python
from crawler.models import FetchResult, FetchStatus

@dataclass
class FetchResult:
    requested_url: str          # Original requested URL
    final_url: str              # Target URL after redirects
    status: FetchStatus         # SUCCESS, NOT_MODIFIED, HTTP_ERROR, NETWORK_ERROR, ROBOTS_BLOCKED, etc.
    status_code: int | None     # e.g., 200, 304, 404
    headers: dict[str, str]     # Case-insensitive response headers
    body: bytes | None          # Raw body bytes (None on 304 or failure)
    text: str | None            # Decoded text content
    content_type: str           # MIME type (e.g. 'text/html')
    encoding: str               # Detected encoding (e.g. 'utf-8')
    fetch_timestamp_utc: datetime
    error: FetchError | None    # Detailed error type and message
    retry_count: int            # Retries performed
    not_modified: bool          # True if HTTP 304
    etag: str | None            # Validator string
    last_modified: str | None   # Validator date string
    latency_ms: float           # Latency in milliseconds
    depth: int                  # Discovery depth
    metadata: dict[str, Any]    # Custom task/source metadata
```

#### Person B Integration Example
```python
from crawler import CrawlerEngine, CrawlerConfig, FetchResult

config = CrawlerConfig()
engine = CrawlerEngine(config)

# Option A: Push / Callback Pattern
def my_pipeline_handler(result: FetchResult) -> None:
    if result.is_success and not result.not_modified:
        # Pass result.text or result.body to Person B's content extractor
        print(f"Extracting content from: {result.final_url}")
    elif result.not_modified:
        print(f"Content unchanged: {result.final_url}")

engine.set_pipeline_handler(my_pipeline_handler)

# Option B: Pull / Async Stream Pattern
async def consume_stream():
    await engine.add_seed_url("https://example.com")
    async for result in engine.run_generator():
        process_in_pipeline(result)
```

---

### Contract with Person C: Orchestration, API & Dashboard
Person C receives real-time crawl lifecycle events and controls engine execution.

#### `CrawlEvent` Model
```python
from crawler.models import CrawlEvent, CrawlEventType

# Event types:
# URL_DISCOVERED, FETCH_START, FETCH_SUCCESS, NOT_MODIFIED, RETRY, FAILURE, ROBOTS_BLOCKED, RATE_LIMITED

@dataclass(frozen=True)
class CrawlEvent:
    event_type: CrawlEventType
    url: str
    timestamp_utc: datetime
    status_code: int | None
    latency_ms: float | None
    retry_count: int
    message: str | None
    details: dict[str, Any]
```

#### Person C Integration Example
```python
from crawler import CrawlerEngine, CrawlEvent

engine = CrawlerEngine()

# Dashboard / Metrics Listener
async def dashboard_listener(event: CrawlEvent) -> None:
    # Update Prometheus metrics, WebSocket dashboard, or log stream
    print(f"[{event.event_type.value}] {event.url} - {event.status_code}")

engine.add_event_listener(dashboard_listener)

# Orchestration Controls
await engine.add_seed_url("https://example.com")
asyncio.create_task(engine.run())

# Person C can pause, resume, or gracefully stop:
engine.pause()
engine.resume()
await engine.stop()

# Query Structured Metrics & Counters (for REST API / Prometheus / Dashboard)
metrics = engine.get_metrics()
print(metrics)
# Returns:
# {
#   "pages_crawled": 4,
#   "pages_succeeded": 3,
#   "pages_failed": 0,
#   "pages_not_modified": 0,
#   "pages_robots_blocked": 1,
#   "pages_ssrf_blocked": 0,
#   "retries_total": 0,
#   "urls_discovered": 5,
#   "queue_pending": 0,
#   "uptime_sec": 1.45,
#   "total_response_time_ms": 452.0,
#   "average_response_time_ms": 113.0,
#   "is_running": False,
#   "is_paused": False
# }
```

---

## 4. Installation & Setup

### Prerequisites
- Python 3.11 or newer

### Setup
```bash
# 1. Clone repository and navigate to directory
cd c:\Users\user\Downloads\DWM

# 2. Install dependencies (either via pip requirements or editable package install)
pip install -r requirements.txt
# Or:
pip install -e ".[dev]"
```

### Environment Configuration
Copy `.env.example` to `.env` if custom parameters are needed:
```bash
copy .env.example .env
```

Key environment variables:
| Variable | Default | Description |
|---|---|---|
| `CRAWLER_USER_AGENT` | `AcademicCrawler/1.0 (+...)` | Identified bot user-agent |
| `CRAWLER_CONTACT_EMAIL` | `crawler-contact@...` | Operator contact address |
| `CRAWLER_MAX_CONCURRENCY` | `10` | Bounded worker concurrency |
| `CRAWLER_PER_HOST_DELAY_SEC` | `1.0` | Minimum throttle delay per host |
| `CRAWLER_REQUEST_TIMEOUT_SEC` | `15.0` | Total request timeout |
| `CRAWLER_MAX_RETRIES` | `3` | Maximum retry attempts for transient errors |
| `CRAWLER_MAX_ROBOTS_SIZE_BYTES` | `524288` | Maximum byte limit for robots.txt (512 KiB) |
| `CRAWLER_ALLOW_PRIVATE_IPS` | `false` | SSRF flag (MUST be false in production) |
| `CRAWLER_STATE_DB_PATH` | `crawl_state.db` | SQLite persistent state file path |

---

## 5. Running Tests

The test suite runs with `pytest` and `pytest-asyncio`. It is completely deterministic and requires no external internet connection:

```bash
# Run all tests (69 passing tests)
python -m pytest -v

# Run specific test modules
python -m pytest tests/test_ssrf_regression.py -v
python -m pytest tests/test_robots_security.py -v
python -m pytest tests/test_fetcher.py -v
python -m pytest tests/test_robots.py -v
python -m pytest tests/test_engine_integration.py -v
```

---

## 6. Running the Demonstration

An automated end-to-end demonstration script is included (`demo.py`). It launches an in-process local HTTP server simulating a real-world multi-page website with robots.txt rules, sitemaps, keyword weighting, and ETag conditional validation:

```bash
python demo.py
```

---

## 7. Security Assumptions & Architectural Disclosures

1. **Client-Side JavaScript Rendering**: The acquisition module executes standard asynchronous HTTP requests (`aiohttp`) and does not evaluate client-side JavaScript (e.g. Single Page Application hydration). Crawling JavaScript-only pages requires a headless browser layer (Playwright or Puppeteer), which is outside the scope of Person A's HTTP acquisition component.
2. **Rate Limiting Concurrency Model**: Per-host rate limiting is enforced in-memory per worker process via an asynchronous token lock. In a horizontally distributed deployment across multiple nodes, rate limiting should be coordinated using a shared distributed state store (such as Redis) managed by Person C's orchestration layer.
3. **Storage Separation**: Person A maintains a dedicated lightweight SQLite database (`crawl_state.db`) strictly for internal crawl-loop state (recrawl intervals, scheduling timestamps, deduplication records, and ETag/Last-Modified HTTP validators). Downstream page content, parsed HTML, text embeddings, and keyword relevance are persisted independently into PostgreSQL by Person B.
4. **SSRF Safe Mode**: By default, `allow_private_ips` is strictly `False`, blocking any loopback, RFC 1918 private, link-local, or cloud metadata IP addresses at both DNS resolution and socket connection time. It should only ever be set to `True` during controlled local testing environments (such as in `demo.py`).
