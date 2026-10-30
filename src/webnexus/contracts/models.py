"""Versioned data contracts shared by the crawler (A), pipeline/storage (B), and API (C).

These models are the only allowed handoff format between the three subsystems.
Anything that crosses a component boundary must be one of:

    FetchResult     A -> B   (raw retrieval outcome)
    NormalizedRecord B -> C  (clean, deduplicated-ready document)
    CrawlEvent      A/B -> C (log line for the dashboard)
    *Config         C -> A/B (run configuration)

Bump ``CONTRACT_VERSION`` (semver) on any breaking change and note it in
docs/contracts.md.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONTRACT_VERSION = "1.0.0"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
SIMHASH_MAX = (1 << 63) - 1


def utcnow() -> datetime:
    return datetime.now(UTC)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class SourceType(StrEnum):
    NEWS = "news"
    BLOG = "blog"
    ECOMMERCE = "ecommerce"
    RESEARCH = "research"
    GOVERNMENT = "government"


class DocumentKind(StrEnum):
    ARTICLE = "article"
    BLOG_POST = "blog_post"
    PRODUCT = "product"
    PAPER = "paper"
    GOVERNMENT_PAGE = "government_page"
    OTHER = "other"


class DiscoveryStrategy(StrEnum):
    SEED = "seed"
    RSS = "rss"
    SITEMAP = "sitemap"
    LISTING = "listing"
    API = "api"


class KeywordMatchType(StrEnum):
    CONTAINS = "contains"
    WORD = "word"
    PHRASE = "phrase"
    REGEX = "regex"


class CrawlStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TargetStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"
    SKIPPED = "skipped"
    DEAD = "dead"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DuplicateMethod(StrEnum):
    CANONICAL_URL = "canonical_url"
    EXACT_HASH = "exact_hash"
    SIMHASH = "simhash"


class ErrorCode(StrEnum):
    TIMEOUT = "timeout"
    DNS = "dns"
    TLS = "tls"
    CONNECTION = "connection"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    ROBOTS_DISALLOWED = "robots_disallowed"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED_CONTENT = "unsupported_content"
    OTHER = "other"


# --------------------------------------------------------------------------- #
# A -> B
# --------------------------------------------------------------------------- #
class FetchError(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str
    retryable: bool = False
    status_code: int | None = None


class FetchResult(ContractModel):
    """Raw outcome of one HTTP fetch.

    The body is carried as base64 so the model is safe to serialize to JSON
    across process/queue boundaries. Use :meth:`from_bytes` and the ``body`` /
    ``text()`` helpers in-process.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    source_id: UUID | None = None
    url: str
    final_url: str
    canonical_url: str | None = None
    status_code: int
    content_type: str | None = None
    encoding: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body_b64: str = ""
    fetched_at: datetime = Field(default_factory=utcnow)
    duration_ms: int = 0
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: FetchError | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        url: str,
        status_code: int,
        body: bytes = b"",
        **kwargs: Any,
    ) -> FetchResult:
        return cls(
            url=url,
            final_url=kwargs.pop("final_url", url),
            status_code=status_code,
            body_b64=base64.b64encode(body).decode("ascii"),
            **kwargs,
        )

    @property
    def body(self) -> bytes:
        return base64.b64decode(self.body_b64) if self.body_b64 else b""

    def text(self, default_encoding: str = "utf-8") -> str:
        return self.body.decode(self.encoding or default_encoding, errors="replace")

    @property
    def is_ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300 and not self.not_modified

    @property
    def is_not_modified(self) -> bool:
        return self.not_modified or self.status_code == 304


# --------------------------------------------------------------------------- #
# B -> C
# --------------------------------------------------------------------------- #
class NormalizedRecord(ContractModel):
    """Source-independent document produced by the extraction pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    source_id: UUID
    url: str
    canonical_url: str
    kind: DocumentKind = DocumentKind.OTHER
    title: str = ""
    author: str | None = None
    published_at: datetime | None = None
    language: str | None = None
    body_text: str
    summary: str | None = None
    content_hash: str
    simhash: int | None = None
    word_count: int = 0
    keywords: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    raw_ref: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    crawled_at: datetime = Field(default_factory=utcnow)

    @field_validator("content_hash")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        if not _HEX64.match(value):
            raise ValueError("content_hash must be a lowercase sha256 hex digest")
        return value

    @field_validator("simhash")
    @classmethod
    def _validate_simhash(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value <= SIMHASH_MAX:
            raise ValueError("simhash must fit in 63 bits (0..2**63-1)")
        return value

    @field_validator("word_count")
    @classmethod
    def _validate_word_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("word_count cannot be negative")
        return value

    @model_validator(mode="after")
    def _validate_published_at(self) -> Self:
        if self.published_at is not None and self.published_at.tzinfo is None:
            object.__setattr__(self, "published_at", self.published_at.replace(tzinfo=UTC))
        return self


class PersistOutcome(ContractModel):
    """Result of upserting one record; C can surface this in run summaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID | None = None
    status: Literal["inserted", "updated", "unchanged", "duplicate", "skipped"]
    duplicate_of_id: UUID | None = None
    duplicate_method: DuplicateMethod | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- #
# A/B -> C
# --------------------------------------------------------------------------- #
class CrawlEvent(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str = CONTRACT_VERSION
    run_id: UUID | None = None
    source_id: UUID | None = None
    target_id: UUID | None = None
    url: str | None = None
    level: LogLevel = LogLevel.INFO
    event: str
    message: str = ""
    status_code: int | None = None
    duration_ms: int | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# C -> A/B
# --------------------------------------------------------------------------- #
class KeywordRule(ContractModel):
    term: str = Field(min_length=1, max_length=255)
    match_type: KeywordMatchType = KeywordMatchType.WORD
    weight: float = Field(default=1.0, gt=0)
    is_active: bool = True

    @field_validator("term")
    @classmethod
    def _strip_term(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("keyword term cannot be blank")
        return cleaned


class SourceConfig(ContractModel):
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=200)
    base_url: str
    source_type: SourceType
    extractor: str = "generic"
    discovery: DiscoveryStrategy = DiscoveryStrategy.SEED
    seed_urls: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    crawl_delay_seconds: float = Field(default=1.0, ge=0)
    max_pages_per_run: int = Field(default=500, ge=1)
    options: dict[str, Any] = Field(default_factory=dict)


class CrawlRunConfig(ContractModel):
    source_ids: list[UUID] = Field(default_factory=list)
    keywords: list[KeywordRule] = Field(default_factory=list)
    user_agent: str = "WebNexusBot/0.1"
    respect_robots_txt: bool = True
    max_concurrency: int = Field(default=8, ge=1, le=64)
    max_pages_per_run: int = Field(default=500, ge=1)
    schedule_cron: str | None = None
