"""Extractor base class: common metadata, hashing, and record construction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from webnexus.contracts import (
    DocumentKind,
    FetchResult,
    NormalizedRecord,
    SourceConfig,
    SourceType,
    sha256_text,
    simhash_text,
    tokenize,
)
from webnexus.pipeline.html import (
    canonical_href,
    clean_text,
    extract_author,
    extract_main_text,
    extract_published_at,
    extract_summary,
    extract_title,
    html_language,
    jsonld_objects,
    jsonld_text,
    jsonld_types,
    meta_content,
    meta_keywords,
    normalize_body,
    parse_html,
    primary_jsonld,
    strip_html,
)
from webnexus.pipeline.language import detect_language

MAX_TITLE = 2000
MAX_SUMMARY = 2000


def strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


@dataclass(slots=True)
class PageCommon:
    title: str = ""
    author: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    language: str | None = None
    site_name: str | None = None
    canonical: str | None = None
    keywords: list[str] = field(default_factory=list)
    og_type: str | None = None


@dataclass(slots=True)
class ExtractedContent:
    body_text: str = ""
    title: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    language: str | None = None
    kind: DocumentKind | None = None
    keywords: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PageContext:
    soup: BeautifulSoup
    jsonld: list[dict[str, Any]]
    result: FetchResult
    source: SourceConfig
    common: PageCommon


class BaseExtractor:
    name = "generic"
    source_types: tuple[SourceType, ...] = ()
    default_kind = DocumentKind.OTHER
    min_body_chars = 100

    def supports(self, source: SourceConfig) -> bool:
        return source.source_type in self.source_types

    def extract(self, result: FetchResult, source: SourceConfig) -> NormalizedRecord | None:
        if not result.is_ok:
            return None

        html = result.text()
        if not html.strip():
            return None

        soup = parse_html(html)
        objects = jsonld_objects(soup)
        main_entity = primary_jsonld(objects)
        common = PageCommon(
            title=extract_title(soup, main_entity),
            author=extract_author(soup, main_entity),
            published_at=extract_published_at(soup, main_entity),
            summary=extract_summary(soup, main_entity),
            language=html_language(soup),
            site_name=meta_content(soup, "og:site_name"),
            canonical=canonical_href(soup),
            keywords=meta_keywords(soup),
            og_type=meta_content(soup, "og:type"),
        )
        context = PageContext(
            soup=soup, jsonld=objects, result=result, source=source, common=common
        )
        content = self.build_content(context)

        body = normalize_body(content.body_text or extract_main_text(soup))
        if len(body) < self.min_body_chars:
            return None

        source_id = result.source_id or source.id
        if source_id is None:
            raise ValueError(
                "NormalizedRecord requires source_id: set FetchResult.source_id or SourceConfig.id"
            )

        canonical_url = strip_fragment(
            result.canonical_url or common.canonical or result.final_url or result.url
        )

        meta: dict[str, Any] = {
            "site_name": common.site_name,
            "og_type": common.og_type,
            "extractor": self.name,
            "jsonld_type": jsonld_types(main_entity)[0] if main_entity else None,
            "tags": common.keywords,
        }
        meta.update(content.meta)
        meta = {key: value for key, value in meta.items() if value not in (None, "", [], {})}

        published_at = content.published_at or common.published_at
        title = clean_text(content.title or common.title)[:MAX_TITLE]
        summary = clean_text(content.summary or common.summary or "")[:MAX_SUMMARY]
        language = detect_language(body, content.language or common.language)

        return NormalizedRecord(
            source_id=source_id,
            url=result.final_url or result.url,
            canonical_url=canonical_url,
            kind=content.kind or self.default_kind,
            title=title,
            author=clean_text(content.author or common.author or "")[:512] or None,
            published_at=published_at,
            language=language,
            body_text=body,
            summary=summary or None,
            content_hash=sha256_text(body),
            simhash=simhash_text(body),
            word_count=len(tokenize(body)),
            keywords=content.keywords or common.keywords,
            meta=meta,
            etag=result.etag,
            last_modified=result.last_modified,
            crawled_at=result.fetched_at,
        )

    def build_content(self, context: PageContext) -> ExtractedContent:
        return ExtractedContent(body_text=extract_main_text(context.soup))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


def jsonld_description(obj: dict[str, Any] | None) -> str | None:
    text = jsonld_text(obj, "description", "abstract", "text")
    return strip_html(text) if text else None
