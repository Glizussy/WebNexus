"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-28

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

source_type = postgresql.ENUM(
    "news", "blog", "ecommerce", "research", "government", name="source_type", create_type=False
)
crawl_status = postgresql.ENUM(
    "pending", "running", "completed", "failed", "cancelled", name="crawl_status", create_type=False
)
target_status = postgresql.ENUM(
    "pending",
    "in_progress",
    "fetched",
    "not_modified",
    "failed",
    "skipped",
    "dead",
    name="target_status",
    create_type=False,
)
document_kind = postgresql.ENUM(
    "article",
    "blog_post",
    "product",
    "paper",
    "government_page",
    "other",
    name="document_kind",
    create_type=False,
)
duplicate_method = postgresql.ENUM(
    "canonical_url", "exact_hash", "simhash", name="duplicate_method", create_type=False
)
keyword_match_type = postgresql.ENUM(
    "contains", "word", "phrase", "regex", name="keyword_match_type", create_type=False
)
log_level = postgresql.ENUM(
    "debug", "info", "warning", "error", name="log_level", create_type=False
)

ENUMS = (
    source_type,
    crawl_status,
    target_status,
    document_kind,
    duplicate_method,
    keyword_match_type,
    log_level,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=True)

    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("source_type", source_type, nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("crawl_delay_seconds", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("name", name="uq_sources_name"),
    )
    op.create_index("ix_sources_domain", "sources", ["domain"])

    op.create_table(
        "crawl_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "trigger", sa.String(length=32), server_default=sa.text("'manual'"), nullable=False
        ),
        sa.Column("status", crawl_status, server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_crawl_runs_source_id_sources",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_runs"),
    )
    op.create_index("ix_crawl_runs_source_id", "crawl_runs", ["source_id"])
    op.create_index("ix_crawl_runs_status", "crawl_runs", ["status"])

    op.create_table(
        "crawl_targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.Column("depth", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("discovered_from", sa.Text(), nullable=True),
        sa.Column("status", target_status, server_default=sa.text("'pending'"), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column("last_modified", sa.String(length=128), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("retry_count", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_crawl_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_crawl_targets_source_id_sources",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_targets"),
        sa.UniqueConstraint("canonical_url", name="uq_crawl_targets_canonical_url"),
    )
    op.create_index("ix_crawl_targets_source_id", "crawl_targets", ["source_id"])
    op.create_index("ix_crawl_targets_url_hash", "crawl_targets", ["url_hash"])
    op.create_index("ix_crawl_targets_source_status", "crawl_targets", ["source_id", "status"])
    op.create_index(
        "ix_crawl_targets_frontier",
        "crawl_targets",
        ["status", "priority", "next_crawl_at"],
    )

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("kind", document_kind, server_default=sa.text("'other'"), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("author", sa.String(length=512), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("body_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("word_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "keywords",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("raw_ref", sa.Text(), nullable=True),
        sa.Column("etag", sa.String(length=512), nullable=True),
        sa.Column("last_modified", sa.String(length=128), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "crawled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("word_count >= 0", name="ck_documents_word_count_non_negative"),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_documents_source_id_sources",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["crawl_targets.id"],
            name="fk_documents_target_id_crawl_targets",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        sa.UniqueConstraint("canonical_url", name="uq_documents_canonical_url"),
    )
    op.create_index("ix_documents_source_id", "documents", ["source_id"])
    op.create_index("ix_documents_content_hash", "documents", ["content_hash"])
    op.create_index("ix_documents_simhash", "documents", ["simhash"])
    op.create_index("ix_documents_source_published", "documents", ["source_id", "published_at"])
    op.create_index("ix_documents_keywords_gin", "documents", ["keywords"], postgresql_using="gin")
    op.create_index("ix_documents_meta_gin", "documents", ["meta"], postgresql_using="gin")

    op.create_table(
        "document_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "changed_fields",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_versions_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_versions"),
        sa.UniqueConstraint(
            "document_id", "version", name="uq_document_versions_document_id_version"
        ),
    )
    op.create_index("ix_document_versions_document_id", "document_versions", ["document_id"])

    op.create_table(
        "duplicates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("duplicate_of_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicate_url", sa.Text(), nullable=False),
        sa.Column("duplicate_content_hash", sa.String(length=64), nullable=False),
        sa.Column("method", duplicate_method, nullable=False),
        sa.Column("similarity", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "similarity IS NULL OR (similarity >= 0 AND similarity <= 1)",
            name="ck_duplicates_similarity_range",
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"],
            ["documents.id"],
            name="fk_duplicates_duplicate_of_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_duplicates_source_id_sources",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_duplicates"),
        sa.UniqueConstraint(
            "duplicate_of_id",
            "duplicate_url",
            name="uq_duplicates_duplicate_of_id_duplicate_url",
        ),
    )
    op.create_index("ix_duplicates_duplicate_of_id", "duplicates", ["duplicate_of_id"])
    op.create_index("ix_duplicates_source_id", "duplicates", ["source_id"])
    op.create_index("ix_duplicates_duplicate_url", "duplicates", ["duplicate_url"])

    op.create_table(
        "keywords",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("term", sa.String(length=255), nullable=False),
        sa.Column(
            "match_type", keyword_match_type, server_default=sa.text("'word'"), nullable=False
        ),
        sa.Column("weight", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_keywords"),
        sa.UniqueConstraint("term", name="uq_keywords_term"),
    )

    op.create_table(
        "document_keywords",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("keyword_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("occurrences", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name="fk_document_keywords_document_id_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["keyword_id"],
            ["keywords.id"],
            name="fk_document_keywords_keyword_id_keywords",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "keyword_id", name="pk_document_keywords"),
    )

    op.create_table(
        "crawl_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("level", log_level, server_default=sa.text("'info'"), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["crawl_runs.id"],
            name="fk_crawl_logs_run_id_crawl_runs",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name="fk_crawl_logs_source_id_sources",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["crawl_targets.id"],
            name="fk_crawl_logs_target_id_crawl_targets",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_logs"),
    )
    op.create_index("ix_crawl_logs_created_at", "crawl_logs", ["created_at"])
    op.create_index("ix_crawl_logs_level_created_at", "crawl_logs", ["level", "created_at"])
    op.create_index("ix_crawl_logs_run_created_at", "crawl_logs", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_table("crawl_logs")
    op.drop_table("document_keywords")
    op.drop_table("keywords")
    op.drop_table("duplicates")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("crawl_targets")
    op.drop_table("crawl_runs")
    op.drop_table("sources")

    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=True)
