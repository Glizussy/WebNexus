"""add full-text search and relevance

Revision ID: 0002_search
Revises: 0001_initial
Create Date: 2026-10-29

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_search"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

SEARCH_VECTOR_EXPR = (
    "setweight(to_tsvector('simple'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(summary, '')), 'B') || "
    "setweight(to_tsvector('simple'::regconfig, coalesce(body_text, '')), 'C')"
)


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_EXPR, persisted=True),
            nullable=True,
        ),
    )
    op.add_column(
        "documents",
        sa.Column("relevance", sa.Float(), server_default=sa.text("0"), nullable=False),
    )
    op.create_index(
        "ix_documents_search_vector", "documents", ["search_vector"], postgresql_using="gin"
    )
    op.create_index("ix_documents_relevance", "documents", ["relevance"])


def downgrade() -> None:
    op.drop_index("ix_documents_relevance", table_name="documents")
    op.drop_index("ix_documents_search_vector", table_name="documents")
    op.drop_column("documents", "relevance")
    op.drop_column("documents", "search_vector")
