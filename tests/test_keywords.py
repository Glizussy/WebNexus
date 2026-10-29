from __future__ import annotations

import pytest

from webnexus.contracts import KeywordMatchType, KeywordRule
from webnexus.pipeline import KeywordMatcher


def rule(
    term: str,
    match_type: KeywordMatchType = KeywordMatchType.WORD,
    weight: float = 1.0,
    active: bool = True,
) -> KeywordRule:
    return KeywordRule(term=term, match_type=match_type, weight=weight, is_active=active)


def test_word_matching_respects_boundaries() -> None:
    matcher = KeywordMatcher([rule("AI")])
    matches = matcher.match("AI-powered systems use ai every day, but rain is not AI.")
    assert len(matches) == 1
    assert matches[0].occurrences == 3
    assert matcher.is_match("rain") is False


def test_contains_matching_is_substring() -> None:
    matcher = KeywordMatcher([rule("crawl", KeywordMatchType.CONTAINS)])
    matches = matcher.match("Crawlers crawl while crawling.")
    assert matches[0].occurrences == 3


def test_phrase_matching_collapses_whitespace() -> None:
    matcher = KeywordMatcher([rule("web   crawling", KeywordMatchType.PHRASE)])
    assert matcher.match("Research on web crawling continues.")[0].occurrences == 1
    assert matcher.match("web\ncrawling at scale")[0].occurrences == 1
    assert matcher.match("web crawlers") == []


def test_regex_matching_and_weights() -> None:
    matcher = KeywordMatcher([rule(r"\bprices?\s*=\s*\d+", KeywordMatchType.REGEX, weight=2.5)])
    matches = matcher.match("price = 42 and PRICES = 7")
    assert matches[0].occurrences == 2
    assert matches[0].score == 5.0


def test_invalid_regex_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid keyword regex"):
        KeywordMatcher([rule("(unclosed", KeywordMatchType.REGEX)])


def test_inactive_rules_are_ignored() -> None:
    matcher = KeywordMatcher([rule("AI", active=False)])
    assert matcher.rules == []
    assert matcher.match("AI") == []


def test_duplicate_terms_are_deduplicated() -> None:
    matcher = KeywordMatcher([rule("AI", weight=1.0), rule("ai", weight=5.0)])
    assert len(matcher.rules) == 1
    assert matcher.rules[0].weight == 1.0


def test_matches_are_sorted_by_score() -> None:
    matcher = KeywordMatcher(
        [rule("crawler", weight=1.0), rule("data", KeywordMatchType.CONTAINS, weight=2.0)]
    )
    matches = matcher.match("data crawler data")
    assert [match.term for match in matches] == ["data", "crawler"]


def test_empty_text_and_empty_rules() -> None:
    assert KeywordMatcher([]).match("anything") == []
    assert KeywordMatcher([rule("AI")]).match("") == []
