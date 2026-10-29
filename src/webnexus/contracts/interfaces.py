"""Structural interfaces each subsystem implements against."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from webnexus.contracts.models import (
    FetchResult,
    NormalizedRecord,
    PersistOutcome,
    SourceConfig,
)


@runtime_checkable
class Fetcher(Protocol):
    """Owned by A. Must honour robots.txt, rate limits, and conditional GETs."""

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult: ...


@runtime_checkable
class Extractor(Protocol):
    """Owned by B. Pure function: same input always yields the same record."""

    name: str

    def supports(self, source: SourceConfig) -> bool: ...

    def extract(self, result: FetchResult, source: SourceConfig) -> NormalizedRecord | None:
        """Return ``None`` when the page has no usable content for this source."""
        ...


@runtime_checkable
class Sink(Protocol):
    """Owned by B. Must be idempotent: repeated persists cannot create duplicates."""

    def persist(self, record: NormalizedRecord) -> PersistOutcome: ...

    def persist_many(self, records: Iterable[NormalizedRecord]) -> list[PersistOutcome]: ...
