from __future__ import annotations

from typing import Any

from bs4 import Tag

from webnexus.contracts import DocumentKind, SourceType
from webnexus.pipeline.base import BaseExtractor, ExtractedContent, PageContext
from webnexus.pipeline.html import (
    clean_text,
    extract_main_text,
    identifier_value,
    jsonld_names,
    jsonld_text,
    meta_content,
    meta_contents,
    pick_jsonld,
)

RESEARCH_TYPES = (
    "ScholarlyArticle",
    "MedicalScholarlyArticle",
    "Article",
    "Report",
    "Dissertation",
    "Thesis",
)


class ResearchPaperExtractor(BaseExtractor):
    name = "research_paper"
    source_types = (SourceType.RESEARCH,)
    default_kind = DocumentKind.PAPER
    min_body_chars = 60

    def build_content(self, context: PageContext) -> ExtractedContent:
        paper = pick_jsonld(context.jsonld, *RESEARCH_TYPES)
        abstract = (
            jsonld_text(paper, "abstract")
            or meta_content(context.soup, "citation_abstract", "dcterms.abstract", "DC.Description")
            or _abstract_block(context)
            or ""
        )
        main = extract_main_text(context.soup)
        body = f"{abstract}\n{main}" if abstract and abstract not in main else (main or abstract)

        authors = jsonld_names(paper.get("author")) if paper else []
        authors = authors or meta_contents(context.soup, "citation_author", "DC.Creator")

        doi = _normalize_doi(
            identifier_value(paper.get("identifier")) if paper else None
        ) or _normalize_doi(
            meta_content(context.soup, "citation_doi", "DC.identifier", "dc.identifier")
        )

        meta: dict[str, Any] = {
            "authors": authors,
            "doi": doi,
            "venue": _name_of(paper.get("isPartOf"))
            if paper
            else None
            or meta_content(context.soup, "citation_journal_title", "citation_conference_title"),
            "year": context.common.published_at.year if context.common.published_at else None,
            "pdf_url": _content_url(paper.get("encoding")) if paper else None,
        }
        return ExtractedContent(
            body_text=body,
            keywords=context.common.keywords,
            meta=meta,
        )


def _normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    cleaned = cleaned.strip()
    return cleaned.lower() if cleaned.startswith("10.") else None


def _name_of(value: Any) -> str | None:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return jsonld_text(value, "name", "title")
    if isinstance(value, list):
        for item in value:
            found = _name_of(item)
            if found:
                return found
    return None


def _content_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("contentUrl", "url", "@id"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    if isinstance(value, list):
        for item in value:
            found = _content_url(item)
            if found:
                return found
    return None


def _abstract_block(context: PageContext) -> str | None:
    for selector in ("#abstract", "section.abstract", "[itemprop='abstract']", ".abstract"):
        tag = context.soup.select_one(selector)
        if isinstance(tag, Tag):
            text = clean_text(tag.get_text(" "))
            if text:
                return text
    return None
