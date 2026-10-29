"""Idempotent Postgres sink with exact + near duplicate elimination and versioning."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from webnexus.contracts import (
    DuplicateMethod,
    NormalizedRecord,
    PersistOutcome,
    utcnow,
)
from webnexus.pipeline.dedup import DedupConfig, DedupEngine
from webnexus.pipeline.keywords import KeywordMatch, KeywordMatcher
from webnexus.storage.models import (
    Document,
    DocumentKeyword,
    DocumentVersion,
    Duplicate,
    Keyword,
)


class PostgresSink:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        matcher: KeywordMatcher | None = None,
        dedup_config: DedupConfig | None = None,
        store_version_snapshots: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._matcher = matcher
        self._dedup_config = dedup_config or DedupConfig()
        self._store_snapshots = store_version_snapshots

    def persist(self, record: NormalizedRecord) -> PersistOutcome:
        with self._session_factory() as session:
            try:
                return self._persist(session, record)
            except IntegrityError:
                session.rollback()
                return self._persist(session, record)

    def persist_many(self, records: Iterable[NormalizedRecord]) -> list[PersistOutcome]:
        return [self.persist(record) for record in records]

    def _persist(self, session: Session, record: NormalizedRecord) -> PersistOutcome:
        existing = session.scalar(
            select(Document).where(Document.canonical_url == record.canonical_url)
        )
        if existing is not None:
            outcome = self._update(session, existing, record)
        else:
            outcome = self._insert(session, record)
        session.commit()
        return outcome

    def _update(
        self, session: Session, document: Document, record: NormalizedRecord
    ) -> PersistOutcome:
        now = utcnow()
        document.last_seen_at = now
        document.etag = record.etag or document.etag
        document.last_modified = record.last_modified or document.last_modified

        if document.content_hash == record.content_hash:
            document.title = record.title or document.title
            document.author = record.author or document.author
            document.summary = record.summary or document.summary
            document.meta = record.meta
            self._sync_keywords(session, document, record.body_text)
            return PersistOutcome(document_id=document.id, status="unchanged")

        changed = _changed_fields(document, record)
        _apply_record(document, record)
        document.version = (document.version or 1) + 1

        history = DocumentVersion(
            document_id=document.id,
            version=document.version,
            content_hash=record.content_hash,
            changed_fields=changed,
            snapshot=_snapshot(record) if self._store_snapshots else None,
        )
        session.add(history)
        self._sync_keywords(session, document, record.body_text)
        return PersistOutcome(
            document_id=document.id,
            status="updated",
            reason=",".join(changed) or None,
        )

    def _insert(self, session: Session, record: NormalizedRecord) -> PersistOutcome:
        dedup = DedupEngine(session, self._dedup_config)

        exact = dedup.find_exact(record.content_hash)
        if exact is not None:
            self._record_duplicate(session, record, exact, DuplicateMethod.EXACT_HASH, 1.0)
            return PersistOutcome(
                status="duplicate",
                duplicate_of_id=exact.id,
                duplicate_method=DuplicateMethod.EXACT_HASH,
            )

        near = dedup.find_near(record.simhash)
        if near is not None:
            self._record_duplicate(
                session, record, near.document, DuplicateMethod.SIMHASH, near.similarity
            )
            return PersistOutcome(
                status="duplicate",
                duplicate_of_id=near.document.id,
                duplicate_method=DuplicateMethod.SIMHASH,
            )

        now = utcnow()
        document = Document(
            source_id=record.source_id,
            url=record.url,
            canonical_url=record.canonical_url,
            kind=record.kind,
            title=record.title,
            author=record.author,
            published_at=record.published_at,
            language=record.language,
            body_text=record.body_text,
            summary=record.summary,
            content_hash=record.content_hash,
            simhash=record.simhash,
            word_count=record.word_count,
            keywords=record.keywords,
            meta=record.meta,
            raw_ref=record.raw_ref,
            etag=record.etag,
            last_modified=record.last_modified,
            version=1,
            first_seen_at=now,
            last_seen_at=now,
            crawled_at=record.crawled_at,
        )
        session.add(document)
        session.flush()

        session.add(
            DocumentVersion(
                document_id=document.id,
                version=1,
                content_hash=record.content_hash,
                changed_fields=[],
                snapshot=_snapshot(record) if self._store_snapshots else None,
            )
        )
        self._sync_keywords(session, document, record.body_text)
        return PersistOutcome(document_id=document.id, status="inserted")

    def _record_duplicate(
        self,
        session: Session,
        record: NormalizedRecord,
        canonical: Document,
        method: DuplicateMethod,
        similarity: float,
    ) -> None:
        existing = session.scalar(
            select(Duplicate).where(
                Duplicate.duplicate_of_id == canonical.id,
                Duplicate.duplicate_url == record.canonical_url,
            )
        )
        if existing is not None:
            existing.method = method
            existing.similarity = similarity
            existing.duplicate_content_hash = record.content_hash
            return

        session.add(
            Duplicate(
                duplicate_of_id=canonical.id,
                source_id=record.source_id,
                duplicate_url=record.canonical_url,
                duplicate_content_hash=record.content_hash,
                method=method,
                similarity=similarity,
            )
        )

    def _sync_keywords(self, session: Session, document: Document, text: str) -> None:
        if self._matcher is None:
            return

        matches = self._matcher.match(text)
        keep: list[Any] = []
        terms: list[tuple[str, float]] = []

        for match in matches:
            keyword = session.scalar(select(Keyword).where(Keyword.term == match.term))
            if keyword is None:
                keyword = Keyword(
                    term=match.term,
                    match_type=match.match_type,
                    weight=match.weight,
                )
                session.add(keyword)
                session.flush()

            link = session.get(DocumentKeyword, (document.id, keyword.id))
            if link is None:
                session.add(
                    DocumentKeyword(
                        document_id=document.id,
                        keyword_id=keyword.id,
                        score=match.score,
                        occurrences=match.occurrences,
                    )
                )
            else:
                link.score = match.score
                link.occurrences = match.occurrences

            keep.append(keyword.id)
            terms.append((match.term, match.score))

        statement = delete(DocumentKeyword).where(DocumentKeyword.document_id == document.id)
        if keep:
            statement = statement.where(DocumentKeyword.keyword_id.not_in(keep))
        session.execute(statement)

        document.keywords = [
            term for term, _ in sorted(terms, key=lambda item: (-item[1], item[0]))
        ][:25]
        document.relevance = _relevance(matches, document.word_count)


def _changed_fields(document: Document, record: NormalizedRecord) -> list[str]:
    fields = ("title", "author", "published_at", "language", "body_text", "summary")
    return [name for name in fields if getattr(document, name) != getattr(record, name)]


def _apply_record(document: Document, record: NormalizedRecord) -> None:
    document.url = record.url
    document.kind = record.kind
    document.title = record.title
    document.author = record.author
    document.published_at = record.published_at
    document.language = record.language
    document.body_text = record.body_text
    document.summary = record.summary
    document.content_hash = record.content_hash
    document.simhash = record.simhash
    document.word_count = record.word_count
    document.keywords = record.keywords
    document.meta = record.meta
    document.raw_ref = record.raw_ref
    document.etag = record.etag
    document.last_modified = record.last_modified
    document.crawled_at = record.crawled_at


def _snapshot(record: NormalizedRecord) -> dict[str, Any]:
    return {
        "title": record.title,
        "summary": record.summary,
        "body_text": record.body_text,
        "meta": record.meta,
    }


def _relevance(matches: list[KeywordMatch], word_count: int) -> float:
    """Weighted keyword hits per 1,000 words."""
    if not matches:
        return 0.0
    total = sum(match.score for match in matches)
    return round(total * 1000 / max(1, word_count), 4)
