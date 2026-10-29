from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from webnexus.contracts import DocumentKind, SourceType, TargetStatus
from webnexus.storage.models import (
    Base,
    CrawlLog,
    CrawlTarget,
    Document,
    DocumentKeyword,
    Duplicate,
    Keyword,
    Source,
)

EXPECTED_TABLES = {
    "sources",
    "crawl_runs",
    "crawl_targets",
    "documents",
    "document_versions",
    "duplicates",
    "keywords",
    "document_keywords",
    "crawl_logs",
}


def test_all_tables_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_table_has_a_primary_key() -> None:
    for name, table in Base.metadata.tables.items():
        assert table.primary_key.columns, f"{name} is missing a primary key"


def test_documents_canonical_url_is_unique() -> None:
    uniques = {
        constraint.name
        for constraint in Document.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_documents_canonical_url" in uniques


def test_crawl_targets_canonical_url_is_unique() -> None:
    uniques = {
        constraint.name
        for constraint in CrawlTarget.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_crawl_targets_canonical_url" in uniques


def test_dedup_indexes_exist() -> None:
    indexes = {index.name for index in Document.__table__.indexes}
    assert {
        "ix_documents_content_hash",
        "ix_documents_simhash",
        "ix_documents_source_published",
        "ix_documents_keywords_gin",
        "ix_documents_meta_gin",
    } <= indexes


def test_frontier_index_exists() -> None:
    indexes = {index.name for index in CrawlTarget.__table__.indexes}
    assert "ix_crawl_targets_frontier" in indexes


def test_document_hash_columns_types() -> None:
    assert Document.__table__.c.content_hash.type.length == 64
    assert Document.__table__.c.simhash.type.__class__ is sa.BigInteger


def test_enum_columns_are_native_postgres_enums() -> None:
    enum_columns = [
        Source.__table__.c.source_type,
        CrawlTarget.__table__.c.status,
        Document.__table__.c.kind,
        Duplicate.__table__.c.method,
        Keyword.__table__.c.match_type,
        CrawlLog.__table__.c.level,
    ]
    for column in enum_columns:
        assert isinstance(column.type, sa.Enum)
        assert column.type.native_enum is True
        assert column.type.name in {
            "source_type",
            "target_status",
            "document_kind",
            "duplicate_method",
            "keyword_match_type",
            "log_level",
        }


def test_enum_values_match_contract() -> None:
    assert Document.__table__.c.kind.type.enums == [member.value for member in DocumentKind]
    assert Source.__table__.c.source_type.type.enums == [member.value for member in SourceType]
    assert CrawlTarget.__table__.c.status.type.enums == [member.value for member in TargetStatus]


def test_duplicates_store_rejected_candidates_not_documents() -> None:
    columns = set(Duplicate.__table__.columns.keys())
    assert {"duplicate_of_id", "duplicate_url", "duplicate_content_hash", "method"} <= columns
    assert "document_id" not in columns
    uniques = {
        constraint.name
        for constraint in Duplicate.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_duplicates_duplicate_of_id_duplicate_url" in uniques


def test_document_keyword_composite_primary_key() -> None:
    assert set(DocumentKeyword.__table__.primary_key.columns.keys()) == {
        "document_id",
        "keyword_id",
    }


def test_models_compile_for_postgres() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(sa.schema.CreateTable(table).compile(dialect=dialect))
        assert table.name in ddl
