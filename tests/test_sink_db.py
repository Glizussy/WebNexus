"""Database-backed tests for the dedup sink and pipeline service.

Requires a disposable database:

    $env:WEBNEXUS_TEST_DATABASE_URL = "postgresql+psycopg://webnexus:webnexus@localhost:5433/webnexus_test"
    pytest tests/test_sink_db.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from crawler.models import (
    FetchResult as CrawlerFetchResult,
    FetchStatus,
)
from webnexus.contracts import (
    DocumentKind,
    DuplicateMethod,
    ErrorCode,
    FetchError,
    FetchResult,
    KeywordMatchType,
    KeywordRule,
    NormalizedRecord,
    SourceConfig,
    SourceType,
    sha256_text,
    simhash_text,
    tokenize,
)
from webnexus.integration import CrawlerPipelineHandler
from webnexus.pipeline import (
    DedupConfig,
    KeywordMatcher,
    LocalRawStore,
    PipelineService,
    PostgresSink,
)
from webnexus.storage import create_db_engine, create_session_factory
from webnexus.storage.models import (
    Document,
    DocumentKeyword,
    DocumentVersion,
    Duplicate,
    Keyword,
    Source,
)

TEST_URL = os.getenv("WEBNEXUS_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="set WEBNEXUS_TEST_DATABASE_URL to run database tests"
)


@pytest.fixture(scope="module")
def engine():
    env = {**os.environ, "DATABASE_URL": TEST_URL or ""}
    for args in (("downgrade", "base"), ("upgrade", "head")):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    created = create_db_engine(TEST_URL)
    yield created
    created.dispose()


@pytest.fixture()
def factory(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE document_keywords, document_versions, duplicates, "
                "crawl_logs, documents, crawl_targets, keywords, crawl_runs, sources "
                "RESTART IDENTITY CASCADE"
            )
        )
    return create_session_factory(engine)


@pytest.fixture()
def source_id(factory) -> UUID:
    with factory() as session:
        source = Source(
            name=f"source-{uuid4()}",
            base_url="https://example.com",
            domain="example.com",
            source_type=SourceType.NEWS,
        )
        session.add(source)
        session.commit()
        return source.id


def record(
    source_id: UUID,
    body: str,
    *,
    url: str = "https://example.com/articles/1",
    canonical: str | None = None,
    simhash: int | None = None,
    **overrides: object,
) -> NormalizedRecord:
    payload: dict[str, object] = {
        "source_id": source_id,
        "url": url,
        "canonical_url": canonical or url,
        "kind": DocumentKind.ARTICLE,
        "title": "A title",
        "body_text": body,
        "content_hash": sha256_text(body),
        "simhash": simhash_text(body) if simhash is None else simhash,
        "word_count": len(tokenize(body)),
    }
    payload.update(overrides)
    return NormalizedRecord(**payload)


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_first_persist_inserts_document_and_version(factory, source_id) -> None:
    sink = PostgresSink(factory)
    outcome = sink.persist(record(source_id, "alpha beta gamma delta epsilon"))

    assert outcome.status == "inserted"
    assert outcome.document_id is not None

    with factory() as session:
        document = session.get(Document, outcome.document_id)
        assert document is not None
        assert document.version == 1
        assert document.relevance == 0.0
        assert count(session, Document) == 1
        versions = session.scalars(select(DocumentVersion)).all()
        assert [version.version for version in versions] == [1]


def test_repeated_persist_is_unchanged(factory, source_id) -> None:
    sink = PostgresSink(factory)
    first = record(source_id, "alpha beta gamma delta epsilon")
    inserted = sink.persist(first)
    outcome = sink.persist(first)

    assert inserted.status == "inserted"
    assert outcome.status == "unchanged"

    with factory() as session:
        document = session.get(Document, outcome.document_id)
        assert document is not None
        assert document.version == 1
        assert count(session, DocumentVersion) == 1


def test_changed_body_updates_and_versions(factory, source_id) -> None:
    sink = PostgresSink(factory)
    url = "https://example.com/articles/changing"
    first = sink.persist(record(source_id, "alpha beta gamma delta epsilon", url=url))
    outcome = sink.persist(
        record(source_id, "alpha beta gamma delta zeta", url=url, title="Updated title")
    )

    assert outcome.status == "updated"
    assert outcome.document_id == first.document_id

    with factory() as session:
        document = session.get(Document, outcome.document_id)
        assert document is not None
        assert document.version == 2
        assert "zeta" in document.body_text

        versions = session.scalars(select(DocumentVersion).order_by(DocumentVersion.version)).all()
        assert [version.version for version in versions] == [1, 2]
        assert "body_text" in versions[1].changed_fields
        assert "title" in versions[1].changed_fields
        assert versions[1].content_hash == document.content_hash


def test_exact_duplicate_is_recorded_not_stored(factory, source_id) -> None:
    sink = PostgresSink(factory)
    body = "identical content across two pages " * 8
    first = sink.persist(record(source_id, body, url="https://example.com/articles/original"))
    outcome = sink.persist(record(source_id, body, url="https://example.com/articles/copy"))

    assert first.status == "inserted"
    assert outcome.status == "duplicate"
    assert outcome.duplicate_of_id == first.document_id
    assert outcome.duplicate_method is DuplicateMethod.EXACT_HASH

    with factory() as session:
        assert count(session, Document) == 1
        duplicate = session.scalar(select(Duplicate))
        assert duplicate is not None
        assert duplicate.duplicate_url == "https://example.com/articles/copy"
        assert duplicate.method is DuplicateMethod.EXACT_HASH
        assert duplicate.similarity == 1.0


def test_near_duplicate_is_detected_with_simhash(factory, source_id) -> None:
    sink = PostgresSink(factory, dedup_config=DedupConfig(near_duplicate_distance=3))
    first = sink.persist(
        record(
            source_id,
            "A long article about web crawling, polite scheduling, and deduplication. " * 4,
            url="https://example.com/articles/near-original",
        )
    )

    with factory() as session:
        stored = session.scalar(select(Document))
        assert stored is not None and stored.simhash is not None
        near_hash = stored.simhash ^ 1

    outcome = sink.persist(
        record(
            source_id,
            "Cooking pasta with tomato sauce and fresh basil is a simple weeknight meal. " * 4,
            url="https://example.com/articles/near-copy",
            simhash=near_hash,
        )
    )

    assert outcome.status == "duplicate"
    assert outcome.duplicate_method is DuplicateMethod.SIMHASH
    assert outcome.duplicate_of_id == first.document_id

    with factory() as session:
        assert count(session, Document) == 1
        duplicate = session.scalar(select(Duplicate))
        assert duplicate is not None
        assert duplicate.method is DuplicateMethod.SIMHASH
        assert duplicate.similarity == pytest.approx(1 - 1 / 63)


def test_keywords_are_linked_and_cleared_on_update(factory, source_id) -> None:
    matcher = KeywordMatcher(
        [KeywordRule(term="crawling", match_type=KeywordMatchType.CONTAINS, weight=2.0)]
    )
    sink = PostgresSink(factory, matcher=matcher)
    url = "https://example.com/articles/keywords"

    first = sink.persist(record(source_id, "crawling data crawling", url=url))

    with factory() as session:
        document = session.get(Document, first.document_id)
        assert document is not None
        assert document.keywords == ["crawling"]
        link = session.scalar(select(DocumentKeyword))
        assert link is not None
        assert link.occurrences == 2
        assert link.score == 4.0
        assert count(session, Keyword) == 1
        assert document.relevance == pytest.approx(4 / 3 * 1000)

    outcome = sink.persist(record(source_id, "data only", url=url))
    assert outcome.status == "updated"

    with factory() as session:
        document = session.get(Document, outcome.document_id)
        assert document is not None
        assert document.keywords == []
        assert document.relevance == 0.0
        assert count(session, DocumentKeyword) == 0
        assert count(session, Keyword) == 1


def test_persist_many(factory, source_id) -> None:
    sink = PostgresSink(factory)
    outcomes = sink.persist_many(
        [
            record(
                source_id,
                "first document body with enough distinct words",
                url="https://example.com/one",
                simhash=1,
            ),
            record(
                source_id,
                "second document body about an unrelated topic",
                url="https://example.com/two",
                simhash=(1 << 40) - 1,
            ),
        ]
    )
    assert [outcome.status for outcome in outcomes] == ["inserted", "inserted"]


def test_pipeline_service_end_to_end(factory, source_id, tmp_path) -> None:
    source = SourceConfig(
        id=source_id,
        name="Example News",
        base_url="https://example.com",
        source_type=SourceType.NEWS,
        extractor="news_article",
    )
    html = (FIXTURES / "news.html").read_text(encoding="utf-8")
    result = FetchResult.from_bytes(
        url="https://example.com/news/ai-crawlers",
        status_code=200,
        body=html.encode("utf-8"),
        encoding="utf-8",
        source_id=source_id,
    )
    sink = PostgresSink(factory)
    service = PipelineService(sink, raw_store=LocalRawStore(tmp_path))

    outcome = service.process(result, source)
    assert outcome.status == "inserted"

    with factory() as session:
        document = session.get(Document, outcome.document_id)
        assert document is not None
        assert document.raw_ref is not None
        assert (tmp_path / document.raw_ref).exists()
        assert document.title == "AI crawlers get smarter"

    assert service.process(result, source).status == "unchanged"

    not_modified = FetchResult.from_bytes(
        url=result.url, status_code=304, not_modified=True, source_id=source_id
    )
    assert service.process(not_modified, source).reason == "not_modified"

    failed = FetchResult.from_bytes(
        url=result.url,
        status_code=504,
        source_id=source_id,
        error=FetchError(code=ErrorCode.TIMEOUT, message="timeout", retryable=True),
    )
    assert service.process(failed, source).reason == "fetch_error:timeout"


def test_crawler_bridge_end_to_end(factory, source_id) -> None:
    config = SourceConfig(
        id=source_id,
        name="Example News",
        base_url="https://example.com",
        source_type=SourceType.NEWS,
        extractor="news_article",
    )
    html = (FIXTURES / "news.html").read_text(encoding="utf-8")
    crawler_result = CrawlerFetchResult(
        requested_url="https://example.com/news/ai-crawlers",
        final_url="https://example.com/news/ai-crawlers",
        status=FetchStatus.SUCCESS,
        status_code=200,
        body=html.encode("utf-8"),
        text=html,
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        latency_ms=42.5,
        metadata={"source_id": str(source_id)},
    )
    handler = CrawlerPipelineHandler(PipelineService(PostgresSink(factory)), config)

    outcome = handler(crawler_result)

    assert outcome.status == "inserted"
    with factory() as session:
        document = session.get(Document, outcome.document_id)
        assert document is not None
        assert document.source_id == source_id
        assert document.canonical_url == "https://example.com/news/ai-crawlers"
        assert document.title == "AI crawlers get smarter"
        assert document.meta["extractor"] == "news_article"

    assert handler(crawler_result).status == "unchanged"
