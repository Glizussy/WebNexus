from __future__ import annotations

from typing import Any

from bs4 import Tag

from webnexus.contracts import DocumentKind, SourceType
from webnexus.pipeline.base import (
    BaseExtractor,
    ExtractedContent,
    PageContext,
    jsonld_description,
)
from webnexus.pipeline.html import (
    clean_text,
    extract_main_text,
    jsonld_text,
    meta_content,
    pick_jsonld,
)


class ProductExtractor(BaseExtractor):
    name = "product"
    source_types = (SourceType.ECOMMERCE,)
    default_kind = DocumentKind.PRODUCT
    min_body_chars = 20

    def build_content(self, context: PageContext) -> ExtractedContent:
        product = pick_jsonld(context.jsonld, "Product", "IndividualProduct")
        offer = _first_offer(product)
        description = jsonld_description(product) or _description_block(context)

        body = description
        if len(clean_text(body)) < self.min_body_chars:
            body = extract_main_text(context.soup)
        elif not body:
            body = extract_main_text(context.soup)

        rating = product.get("aggregateRating") if product else None
        meta: dict[str, Any] = {
            "price": jsonld_text(offer, "price")
            or meta_content(context.soup, "product:price:amount", "og:price:amount"),
            "currency": jsonld_text(offer, "priceCurrency")
            or meta_content(context.soup, "product:price:currency", "og:price:currency"),
            "availability": _availability(jsonld_text(offer, "availability")),
            "brand": _brand(product),
            "sku": jsonld_text(product, "sku", "mpn", "gtin13", "gtin"),
            "rating": jsonld_text(rating, "ratingValue") if isinstance(rating, dict) else None,
            "reviews": jsonld_text(rating, "reviewCount", "ratingCount")
            if isinstance(rating, dict)
            else None,
        }
        return ExtractedContent(
            body_text=body,
            keywords=context.common.keywords,
            meta=meta,
        )


def _first_offer(product: dict[str, Any] | None) -> dict[str, Any] | None:
    if not product:
        return None
    offers = product.get("offers")
    if isinstance(offers, dict):
        return offers
    if isinstance(offers, list):
        for offer in offers:
            if isinstance(offer, dict):
                return offer
    return None


def _availability(value: str | None) -> str | None:
    if not value:
        return None
    return value.rstrip("/").rsplit("/", 1)[-1]


def _brand(product: dict[str, Any] | None) -> str | None:
    if not product:
        return None
    brand = product.get("brand")
    if isinstance(brand, str):
        return clean_text(brand)
    if isinstance(brand, dict):
        return jsonld_text(brand, "name")
    return None


def _description_block(context: PageContext) -> str:
    for selector in ("#description", "[itemprop='description']", ".product-description"):
        tag = context.soup.select_one(selector)
        if isinstance(tag, Tag):
            text = clean_text(tag.get_text(" "))
            if text:
                return text
    return jsonld_description(pick_jsonld(context.jsonld, "Product")) or ""
