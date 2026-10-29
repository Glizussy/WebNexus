from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from crawler.engine import CrawlerEngine
from crawler.models import (
    CrawlEvent as CrawlerCrawlEvent,
    CrawlEventType,
    CrawlRequest,
    FetchError as CrawlerFetchError,
    FetchResult as CrawlerFetchResult,
    FetchStatus,
)
from webnexus.contracts import (
    ErrorCode,
    LogLevel,
    PersistOutcome,
    SourceConfig,
    SourceType,
)
from webnexus.integration import (
    CrawlerPipelineHandler,
    adapt_crawl_event,
    adapt_fetch_result,
    attach_pipeline,
)


def crawler_result(**overrides: object) -> CrawlerFetchResult:
    values: dict[str, object] = {
        "requested_url": "https://example.com/a",
        "final_url": "https://example.com/a",
        "status": FetchStatus.SUCCESS,
        "status_code": 200,
        "body": b"<html><body>hello</body></html>",
        "content_type": "text/html; charset=utf-8",
        "encoding": "utf-8",
    }
    values.update(overrides)
    return CrawlerFetchResult(**values)


def source(source_id: UUID | None = None) -> SourceConfig:
    return SourceConfig(
        id=source_id or uuid4(),
        name="Example",
        base_url="https://example.com",
        source_type=SourceType.NEWS,
        extractor="news_article",
    )


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, SourceConfig]] = []

    def process(self, result, config: SourceConfig) -> PersistOutcome:
        self.calls.append((result, config))
        return PersistOutcome(status="inserted")


class FakeEngine:
    def __init__(self) -> None:
        self.handler = None
        self.listeners = []

    def set_pipeline_handler(self, handler) -> None:
        self.handler = handler

    def add_event_listener(self, listener) -> None:
        self.listeners.append(listener)


def test_success_result_maps_to_pipeline_contract() -> None:
    sid = uuid4()
    result = crawler_result(
        requested_url="https://example.com/a?utm_source=feed#top",
        final_url="https://example.com/a?utm_source=feed#top",
        latency_ms=12.7,
        etag='"abc"',
        headers={"content-type": "text/html"},
        metadata={"source_id": str(sid)},
    )

    mapped = adapt_fetch_result(result)

    assert mapped.url == "https://example.com/a?utm_source=feed#top"
    assert mapped.final_url == "https://example.com/a?utm_source=feed#top"
    assert mapped.canonical_url == "https://example.com/a"
    assert mapped.source_id == sid
    assert mapped.status_code == 200
    assert mapped.duration_ms == 12
    assert mapped.etag == '"abc"'
    assert mapped.body == b"<html><body>hello</body></html>"
    assert mapped.text() == "<html><body>hello</body></html>"
    assert mapped.error is None
    assert mapped.is_ok is True


def test_explicit_source_id_wins_over_metadata() -> None:
    explicit = uuid4()
    metadata = uuid4()
    mapped = adapt_fetch_result(
        crawler_result(metadata={"source_id": str(metadata)}), source_id=explicit
    )
    assert mapped.source_id == explicit


def test_not_modified_maps_to_304_without_error() -> None:
    mapped = adapt_fetch_result(
        crawler_result(status=FetchStatus.NOT_MODIFIED, status_code=None, not_modified=True)
    )
    assert mapped.status_code == 304
    assert mapped.not_modified is True
    assert mapped.is_not_modified is True
    assert mapped.is_ok is False
    assert mapped.error is None


def test_timeout_maps_to_retryable_timeout() -> None:
    mapped = adapt_fetch_result(
        crawler_result(
            status=FetchStatus.TIMEOUT,
            status_code=None,
            error=CrawlerFetchError("ReadTimeout", "read timeout", retryable=True),
        )
    )
    assert mapped.status_code == 0
    assert mapped.error is not None
    assert mapped.error.code is ErrorCode.TIMEOUT
    assert mapped.error.retryable is True
    assert mapped.error.message == "read timeout"


def test_robots_blocked_maps_to_robots_disallowed() -> None:
    mapped = adapt_fetch_result(
        crawler_result(
            status=FetchStatus.ROBOTS_BLOCKED,
            status_code=None,
            error=CrawlerFetchError("RobotsBlocked", "disallowed"),
        )
    )
    assert mapped.error is not None
    assert mapped.error.code is ErrorCode.ROBOTS_DISALLOWED


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        ("ClientConnectorDNSError", ErrorCode.DNS),
        ("SSLCertVerificationError", ErrorCode.TLS),
        ("ClientConnectionResetError", ErrorCode.CONNECTION),
    ],
)
def test_network_errors_are_classified(error_type: str, code: ErrorCode) -> None:
    mapped = adapt_fetch_result(
        crawler_result(
            status=FetchStatus.NETWORK_ERROR,
            status_code=None,
            error=CrawlerFetchError(error_type, "network failure"),
        )
    )
    assert mapped.error is not None
    assert mapped.error.code is code


def test_http_429_maps_to_rate_limited_and_retryable() -> None:
    mapped = adapt_fetch_result(
        crawler_result(
            status=FetchStatus.HTTP_ERROR,
            status_code=429,
            error=CrawlerFetchError("TooManyRequests", "slow down"),
        )
    )
    assert mapped.error is not None
    assert mapped.error.code is ErrorCode.RATE_LIMITED
    assert mapped.error.retryable is True


def test_unsupported_content_maps_to_unsupported() -> None:
    mapped = adapt_fetch_result(
        crawler_result(status=FetchStatus.UNSUPPORTED_CONTENT, status_code=None)
    )
    assert mapped.error is not None
    assert mapped.error.code is ErrorCode.UNSUPPORTED_CONTENT


def test_explicit_canonical_url_on_result_is_respected() -> None:
    result = crawler_result()
    result.canonical_url = "https://example.com/canonical"  # type: ignore[attr-defined]
    assert adapt_fetch_result(result).canonical_url == "https://example.com/canonical"


def test_text_is_used_when_body_is_missing() -> None:
    mapped = adapt_fetch_result(
        crawler_result(body=None, text="plain body", content_type="text/plain")
    )
    assert mapped.body == b"plain body"


def test_invalid_source_id_raises() -> None:
    with pytest.raises(ValueError, match="source_id"):
        adapt_fetch_result(crawler_result(metadata={"source_id": "not-a-uuid"}))


def test_crawl_event_maps_level_and_payload() -> None:
    when = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
    event = CrawlerCrawlEvent(
        event_type=CrawlEventType.FAILURE,
        url="https://example.com/a",
        timestamp_utc=when,
        status_code=503,
        latency_ms=150.4,
        retry_count=2,
        message="server error",
        details={"retryable": True},
    )

    mapped = adapt_crawl_event(event)

    assert mapped.level is LogLevel.ERROR
    assert mapped.event == "failure"
    assert mapped.url == "https://example.com/a"
    assert mapped.status_code == 503
    assert mapped.duration_ms == 150
    assert mapped.message == "server error"
    assert mapped.detail == {"retryable": True}
    assert mapped.created_at == when


def test_crawl_event_robots_level_and_source_override() -> None:
    sid = uuid4()
    event = CrawlerCrawlEvent(event_type=CrawlEventType.ROBOTS_BLOCKED, url="https://example.com/a")
    mapped = adapt_crawl_event(event, source_id=sid)
    assert mapped.level is LogLevel.WARNING
    assert mapped.source_id == sid


def test_handler_uses_configured_source_and_injects_id() -> None:
    config = source()
    service = FakeService()
    handler = CrawlerPipelineHandler(service, config)

    outcome = handler(crawler_result())

    assert outcome.status == "inserted"
    assert len(service.calls) == 1
    mapped, used_source = service.calls[0]
    assert used_source is config
    assert mapped.source_id == config.id


def test_handler_resolves_source_from_metadata() -> None:
    sid = uuid4()
    config = source(sid)
    seen: list[UUID | None] = []

    def resolver(value: UUID | None) -> SourceConfig | None:
        seen.append(value)
        return config if value == sid else None

    service = FakeService()
    handler = CrawlerPipelineHandler(service, source_resolver=resolver)
    outcome = handler(crawler_result(metadata={"source_id": str(sid)}))

    assert outcome.status == "inserted"
    assert seen == [sid]
    assert service.calls[0][1] is config


def test_handler_skips_unknown_source() -> None:
    service = FakeService()
    handler = CrawlerPipelineHandler(service, source_resolver=lambda _: None)
    outcome = handler(crawler_result())
    assert outcome.status == "skipped"
    assert outcome.reason == "unknown_source"
    assert service.calls == []


def test_handler_skips_invalid_source_id() -> None:
    config = SourceConfig(
        id=None,
        name="No id",
        base_url="https://example.com",
        source_type=SourceType.NEWS,
    )
    handler = CrawlerPipelineHandler(FakeService(), config)
    outcome = handler(crawler_result(metadata={"source_id": "broken"}))
    assert outcome.status == "skipped"
    assert outcome.reason is not None and outcome.reason.startswith("invalid_source_id:")


def test_attach_pipeline_wires_handler_and_events() -> None:
    config = source()
    service = FakeService()
    engine = FakeEngine()
    events = []
    handler = attach_pipeline(engine, service, source=config, event_sink=events.append)

    assert engine.handler is handler
    assert len(engine.listeners) == 1

    outcome = engine.handler(crawler_result())
    assert outcome.status == "inserted"

    engine.listeners[0](
        CrawlerCrawlEvent(event_type=CrawlEventType.FETCH_SUCCESS, url="https://example.com/a")
    )
    assert len(events) == 1
    assert events[0].event == "fetch_success"
    assert events[0].source_id == config.id


def test_engine_propagates_request_source_id() -> None:
    sid = uuid4()
    request = CrawlRequest(url="https://example.com/a", source_id=str(sid))
    result = crawler_result(metadata={})

    CrawlerEngine._propagate_source_id(request, result)

    assert result.metadata["source_id"] == str(sid)
