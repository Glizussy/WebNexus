# Database schema

PostgreSQL 16. Source of truth is `src/webnexus/storage/models.py`; the initial
migration is `migrations/versions/0001_initial_schema.py`.

```
sources 1---n crawl_targets 1---1 documents 1---n document_versions
   |                |                 |
   |                |                 +---n document_keywords n---1 keywords
   |                +---n crawl_logs  |
   +---n crawl_runs -+---------------+---n duplicates (self-referential)
```

## Tables

| Table | Purpose |
|---|---|
| `sources` | One row per configured website. Holds type, base URL, crawl delay, extractor options. |
| `crawl_runs` | One row per crawl execution: status, trigger, timing, stats JSON. |
| `crawl_targets` | URL frontier mirror. Unique canonical URL, priority, retry count, ETag/Last-Modified and `next_crawl_at` for incremental crawling. |
| `documents` | Normalized pages. Unique canonical URL, content hash, SimHash, metadata, keyword array, version counter. |
| `document_versions` | Change history: only `content_hash`, `changed_fields`, optional snapshot — not full bodies. |
| `duplicates` | Rejected duplicate candidates (URL + hash + method + similarity) pointing at the surviving document. The duplicate itself is never inserted into `documents`. |
| `keywords` | Configurable search terms with match type and weight. |
| `document_keywords` | Many-to-many with score and occurrence count. |
| `crawl_logs` | Append-only structured events; dashboard reads errors and run timelines from here. |

## Deduplication (three layers)

1. **URL level** — `crawl_targets.canonical_url` and `documents.canonical_url` are
   UNIQUE. The URL frontier never enqueues the same canonical URL twice.
2. **Exact content** — `content_hash` (SHA-256 of normalized text) is indexed.
   If a different URL produces the same hash, insert a `duplicates` row with
   `method = exact_hash` and keep the first-seen document.
3. **Near content** — `simhash` is indexed; candidates come from a Hamming
   distance query, then are confirmed by threshold (default `<= 3` bits) and
   stored as `method = simhash` with `similarity = 1 - distance / 63`.

The pipeline returns `PersistOutcome(status="duplicate", duplicate_of_id=...)`
so the dashboard can report savings without losing provenance.

## Incremental updates

- `crawl_targets` stores `etag`, `last_modified`, `content_hash`, `retry_count`,
  `next_crawl_at`. A re-crawl sends conditional headers; a 304 sets
  `status = not_modified` and does not create a document version.
- On a 200, B compares the new `content_hash` with `documents.content_hash`:
  - same hash -> `unchanged`: bump `last_seen_at` only.
  - different -> `updated`: update the document, increment `version`, append a
    `document_versions` row listing changed fields.
- Full-body history is optional; enable `document_versions.snapshot` only when
  the team agrees on retention.

## Indexing rationale

- `ix_crawl_targets_frontier (status, priority, next_crawl_at)` drives the
  "what to crawl next" query.
- `ix_documents_source_published (source_id, published_at)` supports the
  dashboard's per-source latest-documents view.
- GIN on `documents.keywords` and `documents.meta` supports keyword/report
  filtering without a separate search cluster.
- `ix_crawl_logs_level_created_at (level, created_at)` backs the error feed.

## Dashboard queries this schema supports

```sql
-- errors in the last 24h
SELECT created_at, source_id, url, event, message
FROM crawl_logs
WHERE level = 'error' AND created_at > now() - interval '24 hours'
ORDER BY created_at DESC
LIMIT 100;

-- per-source freshness
SELECT s.name, count(d.id) AS docs, max(d.last_seen_at) AS last_seen
FROM sources s LEFT JOIN documents d ON d.source_id = s.id
GROUP BY s.name ORDER BY docs DESC;

-- dedup savings
SELECT method, count(*) FROM duplicates GROUP BY method;
```
