# WebNexus

Multi-source web crawler: fetch, normalize, deduplicate, store, and report on
data from news sites, blogs, e-commerce, research repositories, and public
government sites.

## Status

| Area | State |
|---|---|
| Acquisition (`crawler/`, Person A) | Fetching, robots.txt (RFC 9309), per-host rate limits, SSRF defense, URL frontier, scheduler, SQLite crawl state, retries. 69 tests. |
| Pipeline + storage (`src/webnexus/`, Person B) | Versioned contracts, Postgres schema + Alembic, five source extractors, keyword matching, exact/SimHash dedup, raw body store. 65 tests incl. Postgres. |
| API/dashboard (`api/`, `web/`, Person C) | Not yet implemented. |

The two halves are merged in one repo but still speak **different FetchResult
models**: `crawler.models.FetchResult` (dataclass, produced by A) and
`webnexus.contracts.FetchResult` (Pydantic, consumed by B). An adapter is the
next integration task; see `docs/contracts.md` for the target shape.

## Layout

```
crawler/                     Person A: async acquisition engine
  engine.py                  CrawlerEngine + set_pipeline_handler / run_generator
  fetcher.py                 aiohttp fetcher, retries, conditional GET
  robots.py                  RFC 9309 robots.txt cache and evaluator
  ssrf.py                    private/link-local/metadata IP protection
  url_manager.py             canonicalization + priority frontier
  scheduler.py               incremental recrawl scheduling
  state.py                   SQLite crawl-state store

src/webnexus/                Person B: pipeline + storage
  contracts/                 shared Pydantic handoffs (v1.0.0)
  pipeline/                  extractors, keywords, dedup, sink, service
  storage/                   SQLAlchemy models, engine/session helpers

migrations/                  Alembic migrations (initial schema)
docs/                        contracts, schema, and acquisition reference
tests/                       A + B test suites
demo.py                      Person A local mock-server demo
```

Acquisition state lives in SQLite (`crawl_state.db`); collected documents live
in PostgreSQL. If the team wants a single database, implement the
`CrawlStateStore` protocol in `crawler/state.py` against `crawl_targets`.

## Quickstart

```powershell
docker compose up -d db
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
alembic upgrade head
pytest
```

Postgres listens on `localhost:5433`. Run the database-backed tests with:

```powershell
docker compose exec -T db psql -U webnexus -d postgres -c "CREATE DATABASE webnexus_test;"
$env:WEBNEXUS_TEST_DATABASE_URL = "postgresql+psycopg://webnexus:webnexus@localhost:5433/webnexus_test"
pytest
```

Crawler-only demo:

```powershell
python demo.py
```

## Contracts

- `docs/contracts.md` — `FetchResult`, `NormalizedRecord`, `CrawlEvent`, config,
  versioning rules, and JSON examples.
- `docs/schema.md` — table reference, three-layer dedup, incremental updates,
  and dashboard queries.
- `docs/acquisition.md` — Person A's detailed module reference and integration
  examples.

## Ownership

| Person | Area |
|---|---|
| A | `crawler/` — acquisition, robots, rate limits, frontier, scheduler |
| B | `src/webnexus/pipeline/`, `src/webnexus/storage/`, `src/webnexus/contracts/` |
| C | `api/`, `web/`, orchestration, dashboard, ops |

Contracts in `src/webnexus/contracts/` and `crawler/models.py` require team
agreement before changes.
