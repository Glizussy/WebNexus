"""PostgreSQL full-text search tests. Requires WEBNEXUS_TEST_DATABASE_URL."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from webnexus.contracts import DocumentKind, SourceType, sha256_text, simhash_text, tokenize
from webnexus.storage import DocumentSearch, create_db_engine, create_session_factory
from webnexus.storage.models import Document, Source

TEST_URL = os.getenv("WEBNEXUS_TEST_DATABASE_URL")
ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="set WEBNEXUS_TEST_DATABASE_URL to run database tests"
)


@pytest.fixture(scope="module")
def engine():
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": TEST_URL or ""},
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


def make_source(factory, *, name: str = "Example News") -> UUID:
    with factory() as session:
        source = Source(
            name=f"{name}-{uuid4()}",
            base_url="https://example.com",
            domain="example.com",
            source_type=SourceType.NEWS,
        )
        session.add(source)
        session.commit()
        return source.id


def make_document(
    factory,
    source_id: UUID,
    *,
    title: str,
    body: str,
    url: str,
    kind: DocumentKind = DocumentKind.ARTICLE,
    language: str | None = "en",
    published_at: datetime | None = None,
) -> UUID:
    with factory() as session:
        document = Document(
            source_id=source_id,
            url=url,
            canonical_url=url,
            kind=kind,
            title=title,
            body_text=body,
            language=language,
            published_at=published_at,
            content_hash=sha256_text(body),
            simhash=simhash_text(body),
            word_count=len(tokenize(body)),
        )
        session.add(document)
        session.commit()
        return document.id


def search(factory, query: str, **kwargs):
    with factory() as session:
        return DocumentSearch(session).search(query, **kwargs)


def test_search_finds_document_and_snippet(factory) -> None:
    source_id = make_source(factory)
    document_id = make_document(
        factory,
        source_id,
        title="Quantum computing breakthrough",
        body="Quantum computing promises faster optimization for logistics and chemistry.",
        url="https://example.com/quantum",
    )

    hits = search(factory, "quantum computing")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.document_id == document_id
    assert hit.title == "Quantum computing breakthrough"
    assert hit.url == "https://example.com/quantum"
    assert hit.source_id == source_id
    assert hit.rank > 0
    assert "quantum" in hit.snippet.lower()


def test_title_match_outranks_body_only(factory) -> None:
    source_id = make_source(factory)
    title_match = make_document(
        factory,
        source_id,
        title="Quantum computing breakthrough",
        body="A general report about data platforms and storage.",
        url="https://example.com/title-match",
    )
    make_document(
        factory,
        source_id,
        title="A general report",
        body="One mention of quantum computing inside a long body of unrelated text.",
        url="https://example.com/body-match",
    )

    hits = search(factory, "quantum computing")

    assert len(hits) == 2
    assert hits[0].document_id == title_match


def test_filters_and_count(factory) -> None:
    news = make_source(factory)
    other = make_source(factory, name="Other")
    published = datetime(2026, 5, 1, tzinfo=UTC)
    make_document(
        factory,
        news,
        title="Crawler policy update",
        body="New crawler guidance for government websites.",
        url="https://example.com/policy",
        language="en",
        published_at=published,
    )
    make_document(
        factory,
        other,
        title="Rastreador web",
        body="Nuevas reglas para los rastreadores web.",
        url="https://example.com/rastreador",
        language="es",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with factory() as session:
        searcher = DocumentSearch(session)
        by_source = searcher.search("crawler", source_id=news)
        by_kind = searcher.search("crawler", kind=DocumentKind.ARTICLE)
        by_language = searcher.search("crawler", language="es")
        by_date = searcher.search("crawler", published_after=datetime(2026, 4, 1, tzinfo=UTC))
        total = searcher.count("crawler")

    assert len(by_source) == 1
    assert len(by_kind) == 1
    assert len(by_language) == 0
    assert len(by_date) == 1
    assert total == 1


def test_pagination(factory) -> None:
    source_id = make_source(factory)
    for index in range(3):
        make_document(
            factory,
            source_id,
            title=f"Crawler report {index}",
            body=f"Body number {index} about crawler policy.",
            url=f"https://example.com/report-{index}",
        )

    first = search(factory, "crawler", limit=1)
    second = search(factory, "crawler", limit=1, offset=1)

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].document_id != second[0].document_id


def test_invalid_arguments(factory) -> None:
    with pytest.raises(ValueError, match="query"):
        search(factory, "   ")
    with pytest.raises(ValueError, match="limit"):
        search(factory, "crawler", limit=0)
    with pytest.raises(ValueError, match="limit"):
        search(factory, "crawler", limit=101)
    with pytest.raises(ValueError, match="offset"):
        search(factory, "crawler", offset=-1)


def test_update_reindexes_search_vector(factory) -> None:
    source_id = make_source(factory)
    document_id = make_document(
        factory,
        source_id,
        title="Quantum computing breakthrough",
        body="Quantum computing promises faster optimization for logistics.",
        url="https://example.com/reindex",
    )

    with factory() as session:
        document = session.get(Document, document_id)
        assert document is not None
        document.body_text = "Cooking pasta with tomato sauce and fresh basil."
        document.title = "Weeknight cooking"
        session.commit()

    assert search(factory, "quantum") == []
    assert len(search(factory, "cooking")) == 1


def test_no_results(factory) -> None:
    source_id = make_source(factory)
    make_document(
        factory,
        source_id,
        title="Crawler report",
        body="Body about crawler policy.",
        url="https://example.com/no-results",
    )
    assert search(factory, "astrophysics") == []
