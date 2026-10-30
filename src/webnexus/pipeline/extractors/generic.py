from __future__ import annotations

from webnexus.contracts import SourceType
from webnexus.pipeline.base import BaseExtractor, ExtractedContent, PageContext
from webnexus.pipeline.html import extract_main_text


class GenericExtractor(BaseExtractor):
    name = "generic"
    source_types = tuple(SourceType)
    min_body_chars = 100

    def build_content(self, context: PageContext) -> ExtractedContent:
        return ExtractedContent(
            body_text=extract_main_text(context.soup),
            keywords=context.common.keywords,
        )
