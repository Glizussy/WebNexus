"""Configurable keyword matching (contains / word / phrase / regex)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from webnexus.contracts import KeywordMatchType, KeywordRule

_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class KeywordMatch:
    term: str
    match_type: KeywordMatchType
    weight: float
    occurrences: int
    score: float


class KeywordMatcher:
    def __init__(self, rules: Iterable[KeywordRule] = ()) -> None:
        compiled: dict[str, tuple[KeywordRule, re.Pattern[str]]] = {}
        for rule in rules:
            if not rule.is_active:
                continue
            key = rule.term.casefold()
            if key in compiled:
                continue
            compiled[key] = (rule, _compile(rule))
        self._compiled = list(compiled.values())

    @property
    def rules(self) -> list[KeywordRule]:
        return [rule for rule, _ in self._compiled]

    def match(self, text: str) -> list[KeywordMatch]:
        if not text or not self._compiled:
            return []
        matches: list[KeywordMatch] = []
        for rule, pattern in self._compiled:
            occurrences = len(pattern.findall(text))
            if occurrences:
                matches.append(
                    KeywordMatch(
                        term=rule.term,
                        match_type=rule.match_type,
                        weight=rule.weight,
                        occurrences=occurrences,
                        score=occurrences * rule.weight,
                    )
                )
        return sorted(matches, key=lambda item: (-item.score, item.term.casefold()))

    def is_match(self, text: str) -> bool:
        return any(pattern.search(text) for _, pattern in self._compiled)


def _compile(rule: KeywordRule) -> re.Pattern[str]:
    if rule.match_type is KeywordMatchType.REGEX:
        try:
            return re.compile(rule.term, re.IGNORECASE)
        except re.error as exc:  # pragma: no cover - guarded by API validation too
            raise ValueError(f"invalid keyword regex {rule.term!r}: {exc}") from exc

    if rule.match_type is KeywordMatchType.CONTAINS:
        return re.compile(re.escape(rule.term), re.IGNORECASE)

    if rule.match_type is KeywordMatchType.PHRASE:
        escaped = re.escape(_WS.sub(" ", rule.term))
        pattern = escaped.replace(r"\ ", r"\s+")
        return re.compile(pattern, re.IGNORECASE)

    pattern = r"(?<!\w)" + re.escape(rule.term) + r"(?!\w)"
    return re.compile(pattern, re.IGNORECASE)
