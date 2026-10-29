"""Glue from a fetch outcome to an extracted, deduplicated, stored document."""

from __future__ import annotations

from webnexus.contracts import (
    FetchResult,
    NormalizedRecord,
    PersistOutcome,
    Sink,
    SourceConfig,
)
from webnexus.pipeline.extractors import build_default_registry
from webnexus.pipeline.raw_store import RawStore
from webnexus.pipeline.registry import ExtractorRegistry


class PipelineService:
    def __init__(
        self,
        sink: Sink,
        *,
        registry: ExtractorRegistry | None = None,
        raw_store: RawStore | None = None,
    ) -> None:
        self._sink = sink
        self._registry = registry or build_default_registry()
        self._raw_store = raw_store

    def process(self, result: FetchResult, source: SourceConfig) -> PersistOutcome:
        if result.error is not None:
            return PersistOutcome(
                status="skipped",
                reason=f"fetch_error:{result.error.code.value}",
            )
        if result.is_not_modified:
            return PersistOutcome(status="skipped", reason="not_modified")
        if not result.is_ok:
            return PersistOutcome(status="skipped", reason=f"http_{result.status_code}")

        extractor = self._registry.for_source(source)
        try:
            record = extractor.extract(result, source)
        except Exception as exc:  # noqa: BLE001 - one bad page must not kill a run
            return PersistOutcome(status="skipped", reason=f"extract_error:{type(exc).__name__}")

        if record is None:
            return PersistOutcome(status="skipped", reason="no_extractable_content")

        return self._sink.persist(self._attach_raw(record, result))

    def process_many(self, results: list[tuple[FetchResult, SourceConfig]]) -> list[PersistOutcome]:
        return [self.process(result, source) for result, source in results]

    def _attach_raw(self, record: NormalizedRecord, result: FetchResult) -> NormalizedRecord:
        if self._raw_store is None:
            return record
        raw_ref = self._raw_store.save(
            crawled_at=record.crawled_at,
            content_hash=record.content_hash,
            body=result.body,
        )
        return record.model_copy(update={"raw_ref": raw_ref})
