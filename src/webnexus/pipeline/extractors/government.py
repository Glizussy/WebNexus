from __future__ import annotations

from typing import Any

from webnexus.contracts import DocumentKind, SourceType
from webnexus.pipeline.base import BaseExtractor, ExtractedContent, PageContext
from webnexus.pipeline.html import (
    clean_text,
    extract_main_text,
    identifier_value,
    jsonld_names,
    jsonld_text,
    meta_content,
    pick_jsonld,
)

GOV_TYPES = (
    "GovernmentService",
    "GovernmentOrganization",
    "Legislation",
    "WebPage",
    "Article",
    "Report",
    "Dataset",
    "FAQPage",
)


class GovernmentPageExtractor(BaseExtractor):
    name = "government_page"
    source_types = (SourceType.GOVERNMENT,)
    default_kind = DocumentKind.GOVERNMENT_PAGE
    min_body_chars = 60

    def build_content(self, context: PageContext) -> ExtractedContent:
        page = pick_jsonld(context.jsonld, *GOV_TYPES)
        body = extract_main_text(context.soup)

        agency = ""
        if page:
            agency = ", ".join(jsonld_names(page.get("publisher"))) or ", ".join(
                jsonld_names(page.get("author"))
            )
        agency = agency or meta_content(
            context.soup, "dcterms.publisher", "agency", "author", "og:site_name"
        )

        document_number = (
            identifier_value(page.get("identifier")) if page else None
        ) or meta_content(
            context.soup,
            "dcterms.identifier",
            "document-number",
            "citation_number",
            "citation_paper_id",
        )

        meta: dict[str, Any] = {
            "agency": clean_text(agency) or None,
            "document_number": document_number,
            "jurisdiction": _name_of(page.get("areaServed")) if page else None,
            "modified_at": jsonld_text(page, "dateModified")
            if page
            else None or meta_content(context.soup, "dcterms.modified", "article:modified_time"),
            "effective_at": jsonld_text(page, "datePublished")
            if page
            else None or meta_content(context.soup, "dcterms.issued", "dcterms.created"),
        }
        return ExtractedContent(
            body_text=body,
            keywords=context.common.keywords,
            meta=meta,
        )


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
