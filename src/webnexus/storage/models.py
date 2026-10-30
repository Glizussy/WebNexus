"""SQLAlchemy schema for the centralized document store.

Design notes
------------
* ``crawl_targets`` mirrors the URL frontier and holds conditional-GET state
  (ETag / Last-Modified) plus retry counters, so incremental crawling survives
  restarts.
* ``documents.canonical_url`` is globally unique: one row per canonical page.
  Exact and near duplicates are *recorded* in ``duplicates`` instead of being
  silently dropped, so the dashboard can report dedup savings.
* ``simhash`` is 63-bit and indexed for Hamming-distance candidate lookup.
* ``crawl_logs`` is append-only and indexed for dashboard queries.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from webnexus.contracts import models as c

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(summary, '')), 'B') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(body_text, '')), 'C')"
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def pg_enum(enum_cls: type[Enum], name: str) -> sa.Enum:
    return sa.Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        native_enum=True,
        validate_strings=True,
    )


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_type: Mapped[c.SourceType] = mapped_column(
        pg_enum(c.SourceType, "source_type"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sa.text("true"), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
    )
    crawl_delay_seconds: Mapped[float] = mapped_column(
        Float, server_default=sa.text("1.0"), nullable=False
    )
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    targets: Mapped[list[CrawlTarget]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list[Document]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(
        String(32), server_default=sa.text("'manual'"), nullable=False
    )
    status: Mapped[c.CrawlStatus] = mapped_column(
        pg_enum(c.CrawlStatus, "crawl_status"),
        default=c.CrawlStatus.PENDING,
        server_default=sa.text("'pending'"),
        nullable=False,
        index=True,
    )
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at()

    source: Mapped[Source | None] = relationship()
    logs: Mapped[list[CrawlLog]] = relationship(back_populates="run", passive_deletes=True)


class CrawlTarget(Base):
    __tablename__ = "crawl_targets"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_crawl_targets_canonical_url"),
        Index("ix_crawl_targets_source_status", "source_id", "status"),
        Index("ix_crawl_targets_frontier", "status", "priority", "next_crawl_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=sa.text("5"), nullable=False)
    depth: Mapped[int] = mapped_column(SmallInteger, server_default=sa.text("0"), nullable=False)
    discovered_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[c.TargetStatus] = mapped_column(
        pg_enum(c.TargetStatus, "target_status"),
        default=c.TargetStatus.PENDING,
        server_default=sa.text("'pending'"),
        nullable=False,
    )
    http_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, server_default=sa.text("0"), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_crawl_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at()
    updated_at: Mapped[datetime] = updated_at()

    source: Mapped[Source] = relationship(back_populates="targets")
    document: Mapped[Document | None] = relationship(back_populates="target", uselist=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_documents_canonical_url"),
        CheckConstraint("word_count >= 0", name="word_count_non_negative"),
        Index("ix_documents_content_hash", "content_hash"),
        Index("ix_documents_simhash", "simhash"),
        Index("ix_documents_source_published", "source_id", "published_at"),
        Index("ix_documents_keywords_gin", "keywords", postgresql_using="gin"),
        Index("ix_documents_meta_gin", "meta", postgresql_using="gin"),
        Index("ix_documents_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_documents_relevance", "relevance"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("crawl_targets.id", ondelete="SET NULL"),
        nullable=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[c.DocumentKind] = mapped_column(
        pg_enum(c.DocumentKind, "document_kind"),
        default=c.DocumentKind.OTHER,
        server_default=sa.text("'other'"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, server_default=sa.text("''"), nullable=False)
    author: Mapped[str | None] = mapped_column(String(512), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa.text("''"))
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(SEARCH_VECTOR_EXPR, persisted=True),
        nullable=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    word_count: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"), nullable=False)
    relevance: Mapped[float] = mapped_column(Float, server_default=sa.text("0"), nullable=False)
    keywords: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=sa.text("'{}'::text[]"), nullable=False
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
    )
    raw_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("1"), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    crawled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = updated_at()

    source: Mapped[Source] = relationship(back_populates="documents")
    target: Mapped[CrawlTarget | None] = relationship(back_populates="document")
    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )
    keyword_links: Mapped[list[DocumentKeyword]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_versions_document_id_version"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=sa.text("'{}'::text[]"), nullable=False
    )
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    captured_at: Mapped[datetime] = created_at()

    document: Mapped[Document] = relationship(back_populates="versions")


class Duplicate(Base):
    """Rejected record kept for audit. The duplicate itself is NOT stored in documents."""

    __tablename__ = "duplicates"
    __table_args__ = (
        UniqueConstraint(
            "duplicate_of_id",
            "duplicate_url",
            name="uq_duplicates_duplicate_of_id_duplicate_url",
        ),
        CheckConstraint(
            "similarity IS NULL OR (similarity >= 0 AND similarity <= 1)",
            name="similarity_range",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    duplicate_of_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    duplicate_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    duplicate_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[c.DuplicateMethod] = mapped_column(
        pg_enum(c.DuplicateMethod, "duplicate_method"), nullable=False
    )
    similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = created_at()


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[uuid.UUID] = uuid_pk()
    term: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    match_type: Mapped[c.KeywordMatchType] = mapped_column(
        pg_enum(c.KeywordMatchType, "keyword_match_type"),
        default=c.KeywordMatchType.WORD,
        server_default=sa.text("'word'"),
        nullable=False,
    )
    weight: Mapped[float] = mapped_column(Float, server_default=sa.text("1.0"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sa.text("true"), nullable=False)
    created_at: Mapped[datetime] = created_at()

    document_links: Mapped[list[DocumentKeyword]] = relationship(
        back_populates="keyword", cascade="all, delete-orphan", passive_deletes=True
    )


class DocumentKeyword(Base):
    __tablename__ = "document_keywords"

    document_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    keyword_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("keywords.id", ondelete="CASCADE"),
        primary_key=True,
    )
    score: Mapped[float] = mapped_column(Float, server_default=sa.text("0"), nullable=False)
    occurrences: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("0"), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="keyword_links")
    keyword: Mapped[Keyword] = relationship(back_populates="document_links")


class CrawlLog(Base):
    __tablename__ = "crawl_logs"
    __table_args__ = (
        Index("ix_crawl_logs_created_at", "created_at"),
        Index("ix_crawl_logs_level_created_at", "level", "created_at"),
        Index("ix_crawl_logs_run_created_at", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, sa.Identity(), primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("crawl_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(as_uuid=True),
        ForeignKey("crawl_targets.id", ondelete="SET NULL"),
        nullable=True,
    )
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[c.LogLevel] = mapped_column(
        pg_enum(c.LogLevel, "log_level"),
        default=c.LogLevel.INFO,
        server_default=sa.text("'info'"),
        nullable=False,
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, server_default=sa.text("''"), nullable=False)
    status_code: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = created_at()

    run: Mapped[CrawlRun | None] = relationship(back_populates="logs")
