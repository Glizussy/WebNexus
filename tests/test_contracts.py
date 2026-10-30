from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from webnexus.contracts import (
    CONTRACT_VERSION,
    CrawlEvent,
    CrawlRunConfig,
    DocumentKind,
    ErrorCode,
    FetchError,
    FetchResult,
    KeywordMatchType,
    KeywordRule,
    LogLevel,
    NormalizedRecord,
    PersistOutcome,
    SourceConfig,
    SourceType,
    hamming_distance,
    near_duplicate,
    sha256_text,
    simhash_text,
)


def test_contract_version_is_declared() -> None:
    assert CONTRACT_VERSION == "1.0.0"


def test_fetch_result_roundtrips_through_json() -> None:
    body = "<html><body>caf\u00e9 \u2014 news</body></html>".encode()
    result = FetchResult.from_bytes(
        url="https://example.com/a",
        final_url="https://example.com/a?redirected=1",
        status_code=200,
        body=body,
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        etag='"abc"',
    )

    restored = FetchResult.model_validate_json(result.model_dump_json())

    assert restored.body == body
    assert restored.text() == body.decode("utf-8")
    assert restored.is_ok is True
    assert restored.is_not_modified is False


def test_fetch_result_body_is_base64_on_the_wire() -> None:
    result = FetchResult.from_bytes(
        url="https://example.com/a",
        status_code=200,
        body=b"\x00\x01binary",
    )
    assert base64.b64decode(result.body_b64) == b"\x00\x01binary"
    assert not result.body_b64.startswith("\x00")


def test_not_modified_result_is_never_ok() -> None:
    result = FetchResult.from_bytes(url="https://example.com/a", status_code=304, not_modified=True)
    assert result.is_ok is False
    assert result.is_not_modified is True


def test_fetch_error_marks_result_failed() -> None:
    result = FetchResult.from_bytes(
        url="https://example.com/a",
        status_code=504,
        error=FetchError(code=ErrorCode.TIMEOUT, message="read timeout", retryable=True),
    )
    assert result.is_ok is False
    assert result.error is not None
    assert result.error.code is ErrorCode.TIMEOUT


def test_fetch_result_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FetchResult(
            url="https://example.com/a",
            final_url="https://example.com/a",
            status_code=200,
            surprise=True,
        )


def _record(**overrides: object) -> NormalizedRecord:
    payload: dict[str, object] = {
        "source_id": uuid4(),
        "url": "https://example.com/a",
        "canonical_url": "https://example.com/a",
        "kind": DocumentKind.ARTICLE,
        "title": "Hello",
        "body_text": "some article body",
        "content_hash": sha256_text("some article body"),
        "simhash": simhash_text("some article body"),
        "word_count": 3,
    }
    payload.update(overrides)
    return NormalizedRecord(**payload)


def test_normalized_record_accepts_valid_payload() -> None:
    record = _record()
    assert record.contract_version == CONTRACT_VERSION
    assert len(record.content_hash) == 64


def test_normalized_record_rejects_bad_hash() -> None:
    with pytest.raises(ValidationError):
        _record(content_hash="not-a-hash")


def test_normalized_record_rejects_simhash_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _record(simhash=1 << 63)


def test_normalized_record_coerces_naive_published_at_to_utc() -> None:
    record = _record(published_at=datetime(2026, 1, 2, 3, 4, 5))
    assert record.published_at is not None
    assert record.published_at.tzinfo is UTC


def test_normalized_record_is_immutable() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.title = "changed"


def test_persist_outcome_shapes() -> None:
    outcome = PersistOutcome(status="inserted", document_id=uuid4())
    assert outcome.duplicate_of_id is None


def test_crawl_event_defaults_to_info() -> None:
    event = CrawlEvent(event="fetch_ok")
    assert event.level is LogLevel.INFO
    assert event.created_at.tzinfo is not None


def test_keyword_rule_trims_and_validates() -> None:
    rule = KeywordRule(term="  AI policy  ", match_type=KeywordMatchType.PHRASE)
    assert rule.term == "AI policy"
    with pytest.raises(ValidationError):
        KeywordRule(term="   ")


def test_source_config_defaults() -> None:
    source = SourceConfig(
        name="Example",
        base_url="https://example.com",
        source_type=SourceType.NEWS,
    )
    assert source.extractor == "generic"
    assert source.crawl_delay_seconds == 1.0
    assert source.allowed_domains == []


def test_run_config_defaults_respect_robots() -> None:
    config = CrawlRunConfig()
    assert config.respect_robots_txt is True
    assert config.max_concurrency == 8


def test_simhash_is_deterministic_and_near_duplicate_aware() -> None:
    base = "the quick brown fox jumps over the lazy dog " * 5
    similar = base + "extra sentence about crawling"
    opposite = "completely unrelated text about cooking pasta and tomato sauce " * 5

    first = simhash_text(base)
    second = simhash_text(base)

    assert first == second
    assert near_duplicate(first, simhash_text(similar), max_distance=10)
    assert hamming_distance(first, simhash_text(opposite)) > 10
    assert 0 <= first <= (1 << 63) - 1


def test_sha256_text_matches_known_value() -> None:
    assert sha256_text("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
