from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from webnexus.contracts import (
    DocumentKind,
    ErrorCode,
    FetchError,
    FetchResult,
    SourceConfig,
    SourceType,
    sha256_text,
    simhash_text,
    tokenize,
)
from webnexus.pipeline import build_default_registry

FIXTURES = Path(__file__).parent / "fixtures"
REGISTRY = build_default_registry()


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_source(
    source_type: SourceType,
    *,
    extractor: str = "generic",
    source_id: UUID | None = None,
) -> SourceConfig:
    return SourceConfig(
        id=source_id or uuid4(),
        name="Test Source",
        base_url="https://example.com",
        source_type=source_type,
        extractor=extractor,
    )


def make_fetch(
    html: str,
    *,
    url: str = "https://example.com/page",
    source_id: UUID | None = None,
    canonical_url: str | None = None,
    status: int = 200,
) -> FetchResult:
    return FetchResult.from_bytes(
        url=url,
        status_code=status,
        body=html.encode("utf-8"),
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        source_id=source_id,
        canonical_url=canonical_url,
    )


def extract(
    fixture: str,
    source_type: SourceType,
    extractor: str,
    *,
    url: str = "https://example.com/page",
    canonical_url: str | None = None,
):
    source = make_source(source_type, extractor=extractor)
    result = make_fetch(load(fixture), url=url, source_id=source.id, canonical_url=canonical_url)
    return REGISTRY.for_source(source).extract(result, source), source, result


def test_registry_lists_expected_extractors() -> None:
    assert REGISTRY.names() == [
        "blog_post",
        "generic",
        "government_page",
        "news_article",
        "product",
        "research_paper",
    ]


def test_registry_prefers_named_extractor_then_type_then_generic() -> None:
    assert REGISTRY.for_source(make_source(SourceType.NEWS, extractor="product")).name == "product"
    assert (
        REGISTRY.for_source(make_source(SourceType.BLOG, extractor="missing")).name == "blog_post"
    )
    assert (
        REGISTRY.for_source(make_source(SourceType.GOVERNMENT, extractor="generic")).name
        == "generic"
    )


def test_news_extraction() -> None:
    record, _, _ = extract("news.html", SourceType.NEWS, "news_article")
    assert record is not None

    assert record.kind is DocumentKind.ARTICLE
    assert record.title == "AI crawlers get smarter"
    assert record.author == "Jane Doe"
    assert record.language == "en"
    assert record.published_at == datetime(2026, 10, 27, 8, 30, tzinfo=UTC)
    assert record.meta["section"] == "Technology"
    assert "web crawling" in record.keywords
    assert record.canonical_url == "https://example.com/news/ai-crawlers?utm_source=feed"
    assert "Home" not in record.body_text
    assert record.content_hash == sha256_text(record.body_text)
    assert record.simhash == simhash_text(record.body_text)
    assert record.word_count == len(tokenize(record.body_text))
    assert record.crawled_at.tzinfo is not None


def test_blog_extraction() -> None:
    record, _, _ = extract("blog.html", SourceType.BLOG, "blog_post")
    assert record is not None
    assert record.kind is DocumentKind.BLOG_POST
    assert record.author == "Sam Rivera"
    assert record.meta["category"] == "Engineering"
    assert "robots.txt" in record.body_text
    assert "Related posts" not in record.body_text


def test_product_extraction() -> None:
    record, _, _ = extract("product.html", SourceType.ECOMMERCE, "product")
    assert record is not None
    assert record.kind is DocumentKind.PRODUCT
    assert record.title == "Acme Wireless Keyboard"
    assert record.meta["price"] == "19.99"
    assert record.meta["currency"] == "USD"
    assert record.meta["availability"] == "InStock"
    assert record.meta["brand"] == "Acme"
    assert record.meta["sku"] == "ACM-KB-01"
    assert record.meta["rating"] == "4.5"
    assert record.meta["reviews"] == "128"
    assert "compact wireless keyboard" in record.body_text


def test_research_extraction() -> None:
    record, _, _ = extract("research.html", SourceType.RESEARCH, "research_paper")
    assert record is not None
    assert record.kind is DocumentKind.PAPER
    assert record.title == "Near-duplicate detection at web scale"
    assert record.meta["doi"] == "10.1234/abc.2026.001"
    assert record.meta["authors"] == ["Ada Lovelace", "Grace Hopper"]
    assert record.meta["venue"] == "Journal of Web Data"
    assert record.meta["year"] == 2026
    assert record.meta["pdf_url"] == "https://repo.example.org/paper.pdf"
    assert "SimHash" in record.body_text


def test_government_extraction() -> None:
    record, _, _ = extract("government.html", SourceType.GOVERNMENT, "government_page")
    assert record is not None
    assert record.kind is DocumentKind.GOVERNMENT_PAGE
    assert record.meta["agency"] == "Department of Testing"
    assert record.meta["document_number"] == "GOV-2026-001"
    assert record.meta["jurisdiction"] == "United States"
    assert "robots directives" in record.body_text


def test_generic_extractor_handles_unknown_layouts() -> None:
    source = make_source(SourceType.BLOG, extractor="generic")
    result = make_fetch(load("blog.html"), source_id=source.id)
    record = REGISTRY.for_source(source).extract(result, source)
    assert record is not None
    assert record.kind is DocumentKind.OTHER
    assert record.meta["extractor"] == "generic"
    assert "Rate limits" in record.body_text


def test_empty_page_yields_no_record_for_every_extractor() -> None:
    html = load("empty.html")
    for extractor in REGISTRY.all():
        source = make_source(SourceType.NEWS, extractor=extractor.name)
        result = make_fetch(html, source_id=source.id)
        assert extractor.extract(result, source) is None


def test_not_modified_and_failed_fetches_yield_no_record() -> None:
    source = make_source(SourceType.NEWS, extractor="news_article")
    not_modified = FetchResult.from_bytes(
        url="https://example.com/a", status_code=304, not_modified=True, source_id=source.id
    )
    failed = FetchResult.from_bytes(
        url="https://example.com/a",
        status_code=504,
        source_id=source.id,
        error=FetchError(code=ErrorCode.TIMEOUT, message="timeout", retryable=True),
    )
    extractor = REGISTRY.for_source(source)
    assert extractor.extract(not_modified, source) is None
    assert extractor.extract(failed, source) is None


def test_source_id_can_come_from_source_config() -> None:
    source = make_source(SourceType.NEWS, extractor="news_article")
    result = make_fetch(load("news.html"))
    record = REGISTRY.for_source(source).extract(result, source)
    assert record is not None
    assert record.source_id == source.id


def test_missing_source_id_is_an_error() -> None:
    source = make_source(SourceType.NEWS, extractor="news_article", source_id=None)
    source = source.model_copy(update={"id": None})
    result = make_fetch(load("news.html"))
    with pytest.raises(ValueError, match="source_id"):
        REGISTRY.for_source(source).extract(result, source)


def test_fetch_canonical_url_wins_over_rel_canonical() -> None:
    record, _, _ = extract(
        "news.html",
        SourceType.NEWS,
        "news_article",
        canonical_url="https://example.com/canonical",
    )
    assert record is not None
    assert record.canonical_url == "https://example.com/canonical"


def test_non_ascii_content_is_decoded() -> None:
    html = (
        "<html lang='fr'><body><article><h1>Résumé</h1>"
        "<p>Les robots doivent respecter les directives et limiter leur débit.</p>"
        "<p>Le contenu reste lisible même avec des accents et des caractères spéciaux.</p>"
        "</article></body></html>"
    )
    source = make_source(SourceType.NEWS, extractor="news_article")
    result = make_fetch(html, source_id=source.id)
    record = REGISTRY.for_source(source).extract(result, source)
    assert record is not None
    assert "Résumé" in record.title
    assert record.language == "fr"
