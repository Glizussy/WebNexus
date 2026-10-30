"""Duplicate detection: canonical URL (DB), exact hash, and SimHash proximity."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import BIT
from sqlalchemy.orm import Session

from webnexus.storage.models import Document


@dataclass(frozen=True, slots=True)
class DedupConfig:
    near_duplicate_distance: int = 3
    candidate_window_days: int = 180
    candidate_limit: int = 200


@dataclass(frozen=True, slots=True)
class NearDuplicate:
    document: Document
    distance: int

    @property
    def similarity(self) -> float:
        return max(0.0, 1.0 - self.distance / 63.0)


class DedupEngine:
    def __init__(self, session: Session, config: DedupConfig | None = None) -> None:
        self._session = session
        self._config = config or DedupConfig()

    def find_exact(self, content_hash: str) -> Document | None:
        return self._session.scalar(
            select(Document)
            .where(Document.content_hash == content_hash)
            .order_by(Document.first_seen_at)
            .limit(1)
        )

    def find_near(self, simhash: int | None) -> NearDuplicate | None:
        if simhash is None:
            return None

        distance = func.bit_count(sa.cast(Document.simhash.bitwise_xor(simhash), BIT(63))).label(
            "distance"
        )
        window = sa.text(f"now() - interval '{int(self._config.candidate_window_days)} days'")
        stmt = (
            select(Document, distance)
            .where(
                Document.simhash.is_not(None),
                Document.first_seen_at >= window,
            )
            .order_by(sa.text("distance"))
            .limit(self._config.candidate_limit)
        )
        row = self._session.execute(stmt).first()
        if row is None:
            return None

        found = int(row[1])
        if found <= self._config.near_duplicate_distance:
            return NearDuplicate(document=row[0], distance=found)
        return None
