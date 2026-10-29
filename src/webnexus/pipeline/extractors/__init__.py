from webnexus.pipeline.base import BaseExtractor
from webnexus.pipeline.extractors.blog import BlogPostExtractor
from webnexus.pipeline.extractors.ecommerce import ProductExtractor
from webnexus.pipeline.extractors.generic import GenericExtractor
from webnexus.pipeline.extractors.government import GovernmentPageExtractor
from webnexus.pipeline.extractors.news import NewsArticleExtractor
from webnexus.pipeline.extractors.research import ResearchPaperExtractor
from webnexus.pipeline.registry import ExtractorRegistry


def default_extractors() -> list[BaseExtractor]:
    return [
        NewsArticleExtractor(),
        BlogPostExtractor(),
        ProductExtractor(),
        ResearchPaperExtractor(),
        GovernmentPageExtractor(),
        GenericExtractor(),
    ]


def build_default_registry() -> ExtractorRegistry:
    return ExtractorRegistry(default_extractors())


__all__ = [
    "BaseExtractor",
    "BlogPostExtractor",
    "ExtractorRegistry",
    "GenericExtractor",
    "GovernmentPageExtractor",
    "NewsArticleExtractor",
    "ProductExtractor",
    "ResearchPaperExtractor",
    "build_default_registry",
    "default_extractors",
]
