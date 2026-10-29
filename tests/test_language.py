from __future__ import annotations

from webnexus.pipeline.language import detect_language, normalize_language

ENGLISH = (
    "The quick brown fox jumps over the lazy dog. Modern web crawlers must respect "
    "robots directives and throttle requests to avoid overloading small websites."
)
SPANISH = (
    "El zorro marron rapido salta sobre el perro perezoso. Los rastreadores web modernos "
    "deben respetar las directivas de robots y limitar las solicitudes para evitar "
    "sobrecargar los sitios web pequenos."
)
FRENCH = (
    "Le renard brun rapide saute par-dessus le chien paresseux. Les robots explorateurs "
    "modernes doivent respecter les directives et limiter les requetes pour ne pas "
    "surcharger les petits sites web."
)


def test_normalize_language() -> None:
    assert normalize_language("EN") == "en"
    assert normalize_language("pt-BR") == "pt-br"
    assert normalize_language("english") is None
    assert normalize_language("  ") is None
    assert normalize_language(None) is None


def test_declared_language_wins() -> None:
    assert detect_language(ENGLISH, "fr") == "fr"
    assert detect_language(ENGLISH, "pt-BR") == "pt-br"


def test_detects_english_spanish_french() -> None:
    assert detect_language(ENGLISH) == "en"
    assert detect_language(SPANISH) == "es"
    assert detect_language(FRENCH) == "fr"


def test_short_text_is_not_guessed() -> None:
    assert detect_language("hello world") is None
    assert detect_language("hi", "de") == "de"


def test_invalid_declared_tag_falls_back_to_detection() -> None:
    assert detect_language(ENGLISH, "not-a-language-code-way-too-long") == "en"
