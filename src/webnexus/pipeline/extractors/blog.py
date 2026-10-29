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

BLOG_TYPES = ("BlogPosting", "Blog", "Article", "LiveBlogPosting")


class BlogPostExtractor(BaseExtractor):
    name = "blog_post"
    source_types = (SourceType.BLOG,)
    default_kind = DocumentKind.BLOG_POST
    min_body_chars = 80

    def build_content(self, context: PageContext) -> ExtractedContent:
        post = pick_jsonld(context.jsonld, *BLOG_TYPES)
        body = extract_main_text(context.soup)
        meta = {
            "category": jsonld_text(post, "articleSection")
            or meta_content(context.soup, "article:section"),
            "modified_at": jsonld_text(post, "dateModified")
            or meta_content(context.soup, "article:modified_time"),
        }
        return ExtractedContent(
            body_text=body,
            keywords=jsonld_keywords(post) or context.common.keywords,
            meta=meta,
        )
