from __future__ import annotations

from webnexus.contracts import DocumentKind, SourceType
from webnexus.pipeline.base import BaseExtractor, ExtractedContent, PageContext
from webnexus.pipeline.html import (
    extract_main_text,
    jsonld_keywords,
    jsonld_text,
    meta_content,
    pick_jsonld,
)

NEWS_TYPES = (
    "NewsArticle",
    "ReportageNewsArticle",
    "AnalysisNewsArticle",
    "Article",
)


class NewsArticleExtractor(BaseExtractor):
    name = "news_article"
    source_types = (SourceType.NEWS,)
    default_kind = DocumentKind.ARTICLE
    min_body_chars = 120

    def build_content(self, context: PageContext) -> ExtractedContent:
        article = pick_jsonld(context.jsonld, *NEWS_TYPES)
        body = extract_main_text(context.soup)
        meta = {
            "section": jsonld_text(article, "articleSection")
            or meta_content(context.soup, "article:section"),
            "modified_at": jsonld_text(article, "dateModified")
            or meta_content(context.soup, "article:modified_time"),
        }
        return ExtractedContent(
            body_text=body,
            keywords=jsonld_keywords(article) or context.common.keywords,
            meta=meta,
        )
