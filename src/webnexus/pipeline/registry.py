"""Extractor registry: name override first, then source type, then generic fallback."""

from __future__ import annotations

from collections.abc import Iterable

from webnexus.contracts import SourceConfig
from webnexus.pipeline.base import BaseExtractor


class ExtractorRegistry:
    def __init__(self, extractors: Iterable[BaseExtractor] = ()) -> None:
        self._by_name: dict[str, BaseExtractor] = {}
        for extractor in extractors:
            self.register(extractor)

    def register(self, extractor: BaseExtractor) -> BaseExtractor:
        if extractor.name in self._by_name:
            raise ValueError(f"extractor name already registered: {extractor.name}")
        self._by_name[extractor.name] = extractor
        return extractor

    def get(self, name: str) -> BaseExtractor | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def all(self) -> list[BaseExtractor]:
        return [self._by_name[name] for name in self.names()]

    def for_source(self, source: SourceConfig) -> BaseExtractor:
        if source.extractor in self._by_name:
            return self._by_name[source.extractor]
        for extractor in self._by_name.values():
            if extractor.supports(source):
                return extractor
        generic = self._by_name.get("generic")
        if generic is None:
            raise LookupError("no extractor matched source and no generic extractor is registered")
        return generic
