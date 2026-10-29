"""PostgreSQL full-text search over stored documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from webnexus.contracts import DocumentKind
from webnexus.storage.models import Document

_SIMPLE = sa.literal_column("'simple'::regconfig")
_MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class SearchHit:
    document_id: UUID
    title: str
    url: str
    source_id: UUID
    published_at: datetime | None
    language: str | None
    relevance: float
    rank: float
    snippet: str


class DocumentSearch:
    def __init__(self, session: Session) -> None:
        self._session = session

    def search(
        self,
        query: str,
        *,
        source_id: UUID | None = None,
        kind: DocumentKind | None = None,
        language: str | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[SearchHit]:
        if not 1 <= limit <= _MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset cannot be negative")

        tsquery = _tsquery(query)
        rank = sa.func.ts_rank_cd(Document.search_vector, tsquery).label("rank")
        snippet = sa.func.ts_headline(
            _SIMPLE,
            Document.body_text,
            tsquery,
            sa.literal_column("'MaxWords=30, MinWords=10, StartSel=<<, StopSel=>>'"),
        ).label("snippet")

        statement = (
            select(
                Document.id,
                Document.title,
                Document.url,
                Document.source_id,
                Document.published_at,
                Document.language,
                Document.relevance,
                rank,
                snippet,
            )
            .where(_conditions(query, source_id, kind, language, published_after, published_before))
            .order_by(
                rank.desc(),
                Document.published_at.desc().nullslast(),
                Document.id,
            )
            .limit(limit)
            .offset(offset)
        )

        return [
            SearchHit(
                document_id=row[0],
                title=row[1],
                url=row[2],
                source_id=row[3],
                published_at=row[4],
                language=row[5],
                relevance=float(row[6] or 0.0),
                rank=float(row[7]),
                snippet=row[8] or "",
            )
            for row in self._session.execute(statement).all()
        ]

    def count(
        self,
        query: str,
        *,
        source_id: UUID | None = None,
        kind: DocumentKind | None = None,
        language: str | None = None,
        published_after: datetime | None = None,
        published_before: datetime | None = None,
    ) -> int:
        statement = (
            select(sa.func.count())
            .select_from(Document)
            .where(_conditions(query, source_id, kind, language, published_after, published_before))
        )
        return int(self._session.scalar(statement) or 0)


def _tsquery(query: str):
    cleaned = (query or "").strip()
    if not cleaned:
        raise ValueError("query must not be blank")
    return sa.func.websearch_to_tsquery(_SIMPLE, cleaned)


def _conditions(
    query: str,
    source_id: UUID | None,
    kind: DocumentKind | None,
    language: str | None,
    published_after: datetime | None,
    published_before: datetime | None,
):
    tsquery = _tsquery(query)
    clauses = [Document.search_vector.op("@@")(tsquery)]
    if source_id is not None:
        clauses.append(Document.source_id == source_id)
    if kind is not None:
        clauses.append(Document.kind == kind)
    if language is not None:
        clauses.append(Document.language == language)
    if published_after is not None:
        clauses.append(Document.published_at >= published_after)
    if published_before is not None:
        clauses.append(Document.published_at <= published_before)
    return sa.and_(*clauses)
