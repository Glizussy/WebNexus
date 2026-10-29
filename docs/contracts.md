# Data contracts (v1.0.0)

The three subsystems only talk through the models in `src/webnexus/contracts/`.
They are Pydantic v2 models with `extra="forbid"`, so any drift fails loudly
instead of being silently ignored.

```
                 FetchResult                 NormalizedRecord
  [ A crawler ] --------------> [ B pipeline ] ----------------> [ C API/UI ]
        ^                              |                              |
        |                              +---- CrawlEvent -------------->|
        +------------------ SourceConfig / CrawlRunConfig ------------+
```

Ownership: A owns `FetchResult` production, B owns `NormalizedRecord`
production, C consumes both and produces run configuration. Anyone may propose
a schema change, but it must be agreed and versioned.

## Rules

1. **Timestamps are UTC and timezone-aware.** `utcnow()` is provided.
2. **`canonical_url` is produced by A** (fragment stripped, tracking params
   removed, host lowercased, trailing-slash policy per domain) and re-used by B.
   B must not invent a different canonical form.
3. **Bodies travel as base64** (`FetchResult.body_b64`) so records survive JSON
   queues. Use `FetchResult.from_bytes(...)` and `.body` / `.text()`.
4. **`content_hash`** is `sha256` of the normalized `body_text` (lowercase hex,
   64 chars). `simhash` is frequency-weighted, 63-bit, so it fits in `BIGINT`.
5. **`NormalizedRecord.meta`** is the escape hatch for source-specific fields
   (price, DOI, agency, tags). Keep stable keys documented per extractor.
6. **Malformed/empty pages** are not errors: return `None` from `Extractor.extract`.
   Network/protocol failures are `FetchResult.error` with a `FetchError`.
7. **Versioning**: additive optional fields = minor bump. Renames, removals, or
   semantic changes to `content_hash`/`canonical_url` = major bump. `1.x`
   consumers must tolerate unknown optional fields being added (use
   `model_validate` with `extra="ignore"` at the boundary if needed).

## FetchResult (A -> B)

`url` is the requested URL; `final_url` is after redirects. `status_code` is
always populated, even when `error` is set. `not_modified` is true for HTTP 304
so B can skip parsing and still refresh `last_seen_at`.

```json
{
  "contract_version": "1.0.0",
  "source_id": "6f1c2a52-5d33-4d55-9a4f-0a4b0f8f6f11",
  "url": "https://example.com/news/ai",
  "final_url": "https://example.com/news/ai",`n  "canonical_url": "https://example.com/news/ai",
  "status_code": 200,
  "content_type": "text/html; charset=utf-8",
  "encoding": "utf-8",
  "headers": {"etag": "\"abc123\""},
  "body_b64": "PGh0bWw+Li4uPC9odG1sPg==",
  "fetched_at": "2026-10-28T09:00:00Z",
  "duration_ms": 184,
  "etag": "\"abc123\"",
  "last_modified": "Tue, 27 Oct 2026 12:00:00 GMT",
  "not_modified": false,
  "error": null
}
```

Error example:

```json
{
  "code": "timeout",
  "message": "read timeout after 20s",
  "retryable": true,
  "status_code": null
}
```

## NormalizedRecord (B -> C)

```json
{
  "contract_version": "1.0.0",
  "source_id": "6f1c2a52-5d33-4d55-9a4f-0a4b0f8f6f11",
  "url": "https://example.com/news/ai",
  "canonical_url": "https://example.com/news/ai",
  "kind": "article",
  "title": "AI crawlers get smarter",
  "author": "Jane Doe",
  "published_at": "2026-10-27T08:30:00Z",
  "language": "en",
  "body_text": "Cleaned article text ...",
  "summary": null,
  "content_hash": "9f2c...64 hex chars...e1",
  "simhash": 3489201188123310000,
  "word_count": 812,
  "keywords": ["web crawling", "deduplication"],
  "meta": {"section": "technology", "og_type": "article"},
  "raw_ref": "raw/2026/10/28/9f2c....html.zst",
  "etag": "\"abc123\"",
  "last_modified": "Tue, 27 Oct 2026 12:00:00 GMT",
  "crawled_at": "2026-10-28T09:00:00Z"
}
```

`PersistOutcome` is returned per record: `inserted`, `updated`, `unchanged`,
`duplicate` (with `duplicate_of_id` + method), or `skipped`.

## CrawlEvent (A/B -> C)

```json
{
  "run_id": "5c0f9d0e-2b7a-4c1e-9d7e-1f2a3b4c5d6e",
  "source_id": "6f1c2a52-5d33-4d55-9a4f-0a4b0f8f6f11",
  "target_id": null,
  "url": "https://example.com/news/ai",
  "level": "error",
  "event": "fetch_failed",
  "message": "read timeout after 20s",
  "status_code": null,
  "duration_ms": 20000,
  "detail": {"retry_count": 2, "retryable": true},
  "created_at": "2026-10-28T09:00:20Z"
}
```

## Configuration (C -> A/B)

`SourceConfig` describes one website: discovery strategy, extractor name,
allowed domains, crawl delay, page budget. `CrawlRunConfig` carries run-level
keywords, concurrency, robots toggle, and an optional cron expression.

```json
{
  "name": "Example News",
  "base_url": "https://example.com",
  "source_type": "news",
  "extractor": "news_article",
  "discovery": "rss",
  "seed_urls": ["https://example.com/feed.xml"],
  "allowed_domains": ["example.com"],
  "crawl_delay_seconds": 1.5,
  "max_pages_per_run": 300,
  "options": {"section_allowlist": ["/news/"]}
}
```

## Open decisions (owner: whole team)

- Canonicalization policy for AMP pages and locale prefixes.
- Near-duplicate threshold (default `hamming <= 3` on 63-bit SimHash).
- Raw HTML retention window and blob backend (local disk vs S3-compatible).
- Language detection library and minimum body length before extraction is attempted.
