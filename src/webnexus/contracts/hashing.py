"""Deterministic hashing helpers for exact and near-duplicate detection."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable

SIMHASH_BITS = 63
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def sha256_text(text: str) -> str:
    """Content fingerprint for exact-duplicate detection."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def simhash64(tokens: Iterable[str], *, bits: int = SIMHASH_BITS) -> int:
    """Frequency-weighted 63-bit SimHash so the value fits in a signed BIGINT."""
    counts = Counter(tokens)
    if not counts:
        return 0

    vector = [0] * bits
    for token, weight in counts.items():
        digest = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for i in range(bits):
            vector[i] += weight if (digest >> i) & 1 else -weight

    fingerprint = 0
    for i, score in enumerate(vector):
        if score > 0:
            fingerprint |= 1 << i
    return fingerprint


def simhash_text(text: str, *, bits: int = SIMHASH_BITS) -> int:
    return simhash64(tokenize(text), bits=bits)


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def near_duplicate(left: int, right: int, *, max_distance: int = 3) -> bool:
    return hamming_distance(left, right) <= max_distance
