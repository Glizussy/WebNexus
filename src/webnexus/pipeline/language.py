"""Language detection with a declared-tag fast path and content fallback."""

from __future__ import annotations

import re

from langdetect import DetectorFactory, LangDetectException, detect

from webnexus.pipeline.html import clean_text

DetectorFactory.seed = 0

MIN_CHARS = 40
MAX_CHARS = 5000
_LANG_TAG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$", re.IGNORECASE)


def normalize_language(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().lower()[:16]
    return candidate if _LANG_TAG.match(candidate) else None


def detect_language(text: str, declared: str | None = None) -> str | None:
    """Return the declared language when valid, otherwise detect from content.

    Detection is skipped for very short texts to avoid noisy guesses.
    """

    declared_language = normalize_language(declared)
    if declared_language:
        return declared_language

    cleaned = clean_text(text)
    if len(cleaned) < MIN_CHARS:
        return None

    try:
        found = detect(cleaned[:MAX_CHARS])
    except LangDetectException:
        return None
    return normalize_language(found)
