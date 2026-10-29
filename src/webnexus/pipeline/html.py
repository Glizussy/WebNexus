"""HTML and structured-data helpers shared by all extractors."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")
_LD_JSON = re.compile(r"application/ld\+json", re.I)

REMOVE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form",
    "nav",
    "footer",
    "aside",
    "button",
)

CONTAINER_SELECTORS = ("article", "main", "[role='main']", ".post-content", ".entry-content")


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return _WS.sub(" ", value.replace("\xa0", " ")).strip()


def strip_html(value: str | None) -> str:
    if not value:
        return ""
    return clean_text(_TAG.sub(" ", value))


def normalize_body(text: str) -> str:
    lines = (clean_text(line) for line in text.replace("\r\n", "\n").split("\n"))
    return "\n".join(line for line in lines if line)


def meta_content(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        for attr in ("property", "name", "itemprop"):
            tag = soup.find("meta", attrs={attr: key})
            if isinstance(tag, Tag):
                content = tag.get("content")
                if isinstance(content, str) and content.strip():
                    return clean_text(content)
    return None


def canonical_href(soup: BeautifulSoup) -> str | None:
    tag = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
    if isinstance(tag, Tag):
        href = tag.get("href")
        if isinstance(href, str) and href.strip():
            return href.strip()
    return None


def html_language(soup: BeautifulSoup) -> str | None:
    tag = soup.find("html")
    if isinstance(tag, Tag):
        lang = tag.get("lang")
        if isinstance(lang, str) and lang.strip():
            return lang.split("-")[0].lower()[:16]
    locale = meta_content(soup, "og:locale")
    if locale:
        return locale.replace("_", "-").split("-")[0].lower()[:16]
    return None


def jsonld_objects(soup: BeautifulSoup) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": _LD_JSON}):
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found.extend(_flatten_jsonld(data))
    return found


def _flatten_jsonld(data: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            out.extend(_flatten_jsonld(item))
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, (list, dict)):
            out.extend(_flatten_jsonld(graph))
        out.append(data)
        entity = data.get("mainEntity")
        if isinstance(entity, dict):
            out.extend(_flatten_jsonld(entity))
    return out


def jsonld_types(obj: dict[str, Any]) -> list[str]:
    value = obj.get("@type")
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, list):
        return [str(item).lower() for item in value]
    return []


def pick_jsonld(objects: list[dict[str, Any]], *types: str) -> dict[str, Any] | None:
    wanted = {item.lower() for item in types}
    for obj in objects:
        if wanted & set(jsonld_types(obj)):
            return obj
    return None


def primary_jsonld(objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best guess at the page's main entity when the type is not known."""

    def score(obj: dict[str, Any]) -> int:
        total = 0
        for key, weight in (
            ("headline", 3),
            ("datePublished", 2),
            ("author", 2),
            ("description", 1),
            ("name", 1),
        ):
            if obj.get(key):
                total += weight
        return total

    if not objects:
        return None
    return max(objects, key=score)


def jsonld_text(obj: dict[str, Any] | None, *keys: str) -> str | None:
    if not obj:
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return clean_text(value)
        if isinstance(value, (int, float)):
            return str(value)
    return None


def jsonld_names(value: Any) -> list[str]:
    """Normalize author/creator values: str, dict, or list of either."""
    if value is None:
        return []
    if isinstance(value, str):
        return [clean_text(value)] if value.strip() else []
    if isinstance(value, dict):
        name = value.get("name") or value.get("legalName")
        return [clean_text(name)] if isinstance(name, str) and name.strip() else []
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            names.extend(jsonld_names(item))
        return names
    return []


def identifier_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("value", "identifier", "@id"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    if isinstance(value, list):
        for item in value:
            found = identifier_value(item)
            if found:
                return found
    return None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def extract_main_text(soup: BeautifulSoup) -> str:
    """Pick the densest content container and return paragraph text.

    Mutates the soup (removes boilerplate tags), so call it after metadata and
    JSON-LD extraction.
    """

    for tag in soup.find_all(REMOVE_TAGS):
        tag.decompose()

    candidates: list[Tag] = []
    for selector in CONTAINER_SELECTORS:
        candidates.extend(tag for tag in soup.select(selector) if isinstance(tag, Tag))

    root = _densest(candidates) if candidates else _densest([soup])
    if root is None:
        return ""

    blocks = [
        clean_text(block.get_text(" "))
        for block in root.find_all(["p", "h2", "h3", "li", "blockquote"])
    ]
    text = "\n".join(block for block in blocks if len(block) >= 2)

    if len(text) < 120:
        text = clean_text(root.get_text(" "))
    return text


def _densest(candidates: list[Tag]) -> Tag | None:
    best: Tag | None = None
    best_score = -1
    for candidate in candidates:
        score = sum(len(clean_text(p.get_text(" "))) for p in candidate.find_all("p"))
        if score > best_score:
            best, best_score = candidate, score
    return best


def extract_title(soup: BeautifulSoup, obj: dict[str, Any] | None = None) -> str:
    fallback = clean_text(soup.title.get_text()) if soup.title else ""
    return (
        jsonld_text(obj, "headline", "name")
        or meta_content(soup, "og:title", "twitter:title")
        or first_text(soup, "h1")
        or fallback
        or ""
    )


def extract_summary(soup: BeautifulSoup, obj: dict[str, Any] | None = None) -> str | None:
    return jsonld_text(obj, "description") or meta_content(
        soup, "og:description", "twitter:description", "description"
    )


def extract_author(soup: BeautifulSoup, obj: dict[str, Any] | None = None) -> str | None:
    if obj:
        names = jsonld_names(obj.get("author")) or jsonld_names(obj.get("creator"))
        if names:
            return ", ".join(dict.fromkeys(names))[:512]
    byline = first_text(soup, ".byline", "[rel='author']", ".author", "[itemprop='author']")
    return byline or meta_content(soup, "author", "article:author")


def extract_published_at(soup: BeautifulSoup, obj: dict[str, Any] | None = None) -> datetime | None:
    if obj:
        for key in ("datePublished", "dateCreated", "uploadDate", "dateModified"):
            parsed = parse_datetime(jsonld_text(obj, key))
            if parsed:
                return parsed
    meta = meta_content(
        soup,
        "article:published_time",
        "datePublished",
        "date",
        "og:published_time",
        "citation_publication_date",
    )
    parsed = parse_datetime(meta)
    if parsed:
        return parsed
    for selector in ("time[datetime]", "time[pubdate]"):
        tag = soup.select_one(selector)
        if isinstance(tag, Tag):
            parsed = parse_datetime(tag.get("datetime") or tag.get_text(" "))
            if parsed:
                return parsed
    return None


def meta_keywords(soup: BeautifulSoup) -> list[str]:
    raw = meta_content(soup, "keywords", "news_keywords")
    values = [part.strip() for part in re.split(r"[,;]", raw or "") if part.strip()]
    for tag in soup.find_all("meta", attrs={"property": "article:tag"}):
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            values.append(clean_text(content))
    return list(dict.fromkeys(values))


def meta_contents(soup: BeautifulSoup, *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        for attr in ("name", "property", "itemprop"):
            for tag in soup.find_all("meta", attrs={attr: key}):
                content = tag.get("content")
                if isinstance(content, str) and content.strip():
                    values.append(clean_text(content))
    return list(dict.fromkeys(values))


def jsonld_keywords(obj: dict[str, Any] | None) -> list[str]:
    if not obj:
        return []
    value = obj.get("keywords")
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r"[,;]", value)]
    elif isinstance(value, list):
        parts = [clean_text(str(item)) for item in value]
    else:
        return []
    return list(dict.fromkeys(part for part in parts if part))


def first_text(soup: BeautifulSoup, *selectors: str) -> str | None:
    for selector in selectors:
        tag = soup.select_one(selector)
        if isinstance(tag, Tag):
            text = clean_text(tag.get_text(" "))
            if text:
                return text
    return None
