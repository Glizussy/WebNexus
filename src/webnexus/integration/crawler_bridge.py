"""Bridge between Person A's acquisition models and Person B's shared contracts.

A's crawler emits ``crawler.models.FetchResult`` dataclasses; the pipeline
consumes ``webnexus.contracts.FetchResult``. This module is the only place that
knows both shapes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from crawler.models import (
    CrawlEvent as CrawlerCrawlEvent,
    CrawlEventType as CrawlerEventType,
    FetchError as CrawlerFetchError,
    FetchResult as CrawlerFetchResult,
    FetchStatus as CrawlerFetchStatus,
)
from crawler.url_manager import canonicalize_url
from webnexus.contracts import (
    CrawlEvent,
    ErrorCode,
    FetchError,
    FetchResult,
    LogLevel,
    PersistOutcome,
    SourceConfig,
)
from webnexus.pipeline.service import PipelineService

SourceResolver = Callable[[UUID | None], SourceConfig | None]

_DEFAULT_ERRORS = {
    CrawlerFetchStatus.HTTP_ERROR: "request failed",
    CrawlerFetchStatus.NETWORK_ERROR: "network error",
    CrawlerFetchStatus.TIMEOUT: "request timed out",
    CrawlerFetchStatus.ROBOTS_BLOCKED: "blocked by robots.txt",
    CrawlerFetchStatus.SSRF_BLOCKED: "blocked by SSRF protection",
    CrawlerFetchStatus.UNSUPPORTED_CONTENT: "unsupported content type",
    CrawlerFetchStatus.TOO_MANY_REDIRECTS: "too many redirects",
    CrawlerFetchStatus.OVERSIZED_RESPONSE: "response exceeded size limit",
    CrawlerFetchStatus.CANCELLED: "request cancelled",
}

_EVENT_LEVELS = {
    CrawlerEventType.URL_DISCOVERED: LogLevel.DEBUG,
    CrawlerEventType.FETCH_START: LogLevel.DEBUG,
    CrawlerEventType.FETCH_SUCCESS: LogLevel.INFO,
    CrawlerEventType.NOT_MODIFIED: LogLevel.INFO,
    CrawlerEventType.RETRY: LogLevel.WARNING,
    CrawlerEventType.FAILURE: LogLevel.ERROR,
    CrawlerEventType.ROBOTS_BLOCKED: LogLevel.WARNING,
    CrawlerEventType.RATE_LIMITED: LogLevel.WARNING,
}


class PipelineHookEngine(Protocol):
    def set_pipeline_handler(self, handler: Callable[[CrawlerFetchResult], Any]) -> None: ...

    def add_event_listener(self, listener: Callable[[CrawlerCrawlEvent], Any]) -> None: ...


def adapt_fetch_result(result: CrawlerFetchResult, *, source_id: UUID | None = None) -> FetchResult:
    """Convert A's fetch outcome into the pipeline contract."""

    resolved_source = _coerce_uuid(
        source_id or _source_id_from_result(result), error_prefix="source_id"
    )
    status = result.status
    not_modified = result.not_modified or status is CrawlerFetchStatus.NOT_MODIFIED
    status_code = _status_code(result, status, not_modified)
    body = _body_bytes(result)

    return FetchResult.from_bytes(
        url=result.requested_url,
        final_url=result.final_url or result.requested_url,
        canonical_url=_canonical(result),
        status_code=status_code,
        body=body,
        content_type=result.content_type or None,
        encoding=result.encoding or None,
        headers=dict(result.headers or {}),
        fetched_at=result.fetch_timestamp_utc,
        duration_ms=int(result.latency_ms or 0),
        etag=result.etag,
        last_modified=result.last_modified,
        not_modified=not_modified,
        source_id=resolved_source,
        error=_adapt_error(result, status, status_code),
    )


def adapt_crawl_event(
    event: CrawlerCrawlEvent, *, run_id: UUID | None = None, source_id: UUID | None = None
) -> CrawlEvent:
    """Convert A's lifecycle event into the dashboard contract."""

    details = dict(event.details or {})
    if run_id is None:
        run_id = _coerce_uuid(details.get("run_id"))
    if source_id is None:
        source_id = _coerce_uuid(details.get("source_id"))

    return CrawlEvent(
        run_id=run_id,
        source_id=source_id,
        url=event.url,
        level=_EVENT_LEVELS.get(event.event_type, LogLevel.INFO),
        event=event.event_type.value,
        message=event.message or "",
        status_code=event.status_code,
        duration_ms=int(event.latency_ms) if event.latency_ms is not None else None,
        detail=details,
        created_at=event.timestamp_utc,
    )


class CrawlerPipelineHandler:
    """Callable suitable for ``CrawlerEngine.set_pipeline_handler``.

    Provide either a single ``source`` (one engine per source) or a
    ``source_resolver`` that maps the result's source id to its ``SourceConfig``.
    """

    def __init__(
        self,
        service: PipelineService,
        source: SourceConfig | None = None,
        *,
        source_resolver: SourceResolver | None = None,
    ) -> None:
        if source is None and source_resolver is None:
            raise ValueError("provide source or source_resolver")
        self._service = service
        self._source = source
        self._source_resolver = source_resolver

    def __call__(self, result: CrawlerFetchResult) -> PersistOutcome:
        raw_source = _source_id_from_result(result)
        source = self._source
        if self._source_resolver is not None:
            resolved = self._source_resolver(_coerce_uuid(raw_source))
            if resolved is not None:
                source = resolved
        if source is None:
            return PersistOutcome(status="skipped", reason="unknown_source")

        try:
            mapped = adapt_fetch_result(result, source_id=source.id)
        except ValueError as exc:
            return PersistOutcome(status="skipped", reason=f"invalid_source_id:{exc}")

        return self._service.process(mapped, source)


def attach_pipeline(
    engine: PipelineHookEngine,
    service: PipelineService,
    *,
    source: SourceConfig | None = None,
    source_resolver: SourceResolver | None = None,
    event_sink: Callable[[CrawlEvent], Any] | None = None,
) -> CrawlerPipelineHandler:
    """Wire an acquisition engine to the pipeline (and optionally to an event sink)."""

    handler = CrawlerPipelineHandler(service, source=source, source_resolver=source_resolver)
    engine.set_pipeline_handler(handler)

    if event_sink is not None:
        fixed_source_id = source.id if source is not None else None

        def listener(event: CrawlerCrawlEvent) -> Any:
            return event_sink(adapt_crawl_event(event, source_id=fixed_source_id))

        engine.add_event_listener(listener)

    return handler


def _source_id_from_result(result: CrawlerFetchResult) -> Any:
    metadata = result.metadata or {}
    return metadata.get("source_id") or getattr(result, "source_id", None)


def _coerce_uuid(value: Any, *, error_prefix: str | None = None) -> UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as exc:
        if error_prefix:
            raise ValueError(f"{error_prefix} must be a UUID, got {value!r}") from exc
        return None


def _status_code(
    result: CrawlerFetchResult,
    status: CrawlerFetchStatus,
    not_modified: bool,
) -> int:
    if result.status_code is not None:
        return result.status_code
    if not_modified:
        return 304
    if status is CrawlerFetchStatus.SUCCESS:
        return 200
    return 0


def _body_bytes(result: CrawlerFetchResult) -> bytes:
    if isinstance(result.body, bytes) and result.body:
        return result.body
    if result.text:
        return result.text.encode(result.encoding or "utf-8", errors="replace")
    return b""


def _canonical(result: CrawlerFetchResult) -> str:
    explicit = getattr(result, "canonical_url", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    target = result.final_url or result.requested_url
    try:
        return canonicalize_url(target)
    except ValueError:
        return target


def _adapt_error(
    result: CrawlerFetchResult,
    status: CrawlerFetchStatus,
    status_code: int,
) -> FetchError | None:
    if status in (CrawlerFetchStatus.SUCCESS, CrawlerFetchStatus.NOT_MODIFIED):
        return None

    crawler_error = result.error
    code = _error_code(status, status_code, crawler_error)
    retryable = bool(crawler_error.retryable) if crawler_error else _retryable(status, status_code)
    if code is ErrorCode.RATE_LIMITED:
        retryable = True
    message = crawler_error.message if crawler_error else _DEFAULT_ERRORS.get(status, status.value)
    return FetchError(
        code=code,
        message=message,
        retryable=retryable,
        status_code=result.status_code,
    )


def _error_code(
    status: CrawlerFetchStatus,
    status_code: int,
    error: CrawlerFetchError | None,
) -> ErrorCode:
    if status is CrawlerFetchStatus.TIMEOUT:
        return ErrorCode.TIMEOUT
    if status is CrawlerFetchStatus.ROBOTS_BLOCKED:
        return ErrorCode.ROBOTS_DISALLOWED
    if status is CrawlerFetchStatus.UNSUPPORTED_CONTENT:
        return ErrorCode.UNSUPPORTED_CONTENT
    if status is CrawlerFetchStatus.NETWORK_ERROR:
        text = f"{error.error_type} {error.message}".lower() if error else ""
        if "dns" in text or "name resolution" in text or "getaddrinfo" in text:
            return ErrorCode.DNS
        if "ssl" in text or "tls" in text or "certificate" in text:
            return ErrorCode.TLS
        return ErrorCode.CONNECTION
    if status is CrawlerFetchStatus.HTTP_ERROR:
        return ErrorCode.RATE_LIMITED if status_code == 429 else ErrorCode.HTTP_ERROR
    if status is CrawlerFetchStatus.TOO_MANY_REDIRECTS:
        return ErrorCode.HTTP_ERROR
    return ErrorCode.OTHER


def _retryable(status: CrawlerFetchStatus, status_code: int) -> bool:
    if status in (CrawlerFetchStatus.TIMEOUT, CrawlerFetchStatus.NETWORK_ERROR):
        return True
    return status is CrawlerFetchStatus.HTTP_ERROR and (
        status_code == 0 or status_code == 429 or status_code >= 500
    )
