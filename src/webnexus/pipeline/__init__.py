from webnexus.pipeline.base import (
    BaseExtractor,
    ExtractedContent,
    PageCommon,
    PageContext,
)
from webnexus.pipeline.dedup import DedupConfig, DedupEngine, NearDuplicate
from webnexus.pipeline.extractors import (
    BlogPostExtractor,
    GenericExtractor,
    GovernmentPageExtractor,
    NewsArticleExtractor,
    ProductExtractor,
    ResearchPaperExtractor,
    build_default_registry,
    default_extractors,
)
from webnexus.pipeline.keywords import KeywordMatch, KeywordMatcher
from webnexus.pipeline.raw_store import LocalRawStore, RawStore
from webnexus.pipeline.registry import ExtractorRegistry
from webnexus.pipeline.service import PipelineService
from webnexus.pipeline.sink import PostgresSink

__all__ = [
    "BaseExtractor",
    "BlogPostExtractor",
    "DedupConfig",
    "DedupEngine",
    "ExtractedContent",
    "ExtractorRegistry",
    "GenericExtractor",
    "GovernmentPageExtractor",
    "KeywordMatch",
    "KeywordMatcher",
    "LocalRawStore",
    "NearDuplicate",
    "NewsArticleExtractor",
    "PageCommon",
    "PageContext",
    "PipelineService",
    "PostgresSink",
    "ProductExtractor",
    "RawStore",
    "ResearchPaperExtractor",
    "build_default_registry",
    "default_extractors",
]
