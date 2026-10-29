# WebNexus

Multi-source web crawler: fetch, normalize, deduplicate, store, and report on
data from news sites, blogs, e-commerce, research repositories, and public
government sites.

## Status

| Area | State |
|---|---|
| Acquisition (`crawler/`, Person A) | Fetching, robots.txt (RFC 9309), per-host rate limits, SSRF defense, URL frontier, scheduler, SQLite crawl state, retries. |
| Pipeline + storage (`src/webnexus/`, Person B) | Versioned contracts, Postgres schema + Alembic, five source extractors, keyword matching, exact/SimHash dedup, raw body store. |
| A → B bridge (`src/webnexus/integration/`) | Converts A's fetch results/events into B's contracts and wires `set_pipeline_handler` → `PipelineService`. |
| API/dashboard (`api/`, `web/`, Person C) | Not yet implemented. |

The two halves use different `FetchResult` models by design:
`crawler.models.FetchResult` (dataclass, produced by A) and
`webnexus.contracts.FetchResult` (Pydantic, consumed by B). The bridge in
`src/webnexus/integration/crawler_bridge.py` is the only code that knows both.

```python
from webnexus.integration import attach_pipeline

attach_pipeline(engine, pipeline_service, source=source_config, event_sink=record_event)
```

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
  integration/               A -> B fetch/event bridge and engine wiring
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

## Wiring the crawler to the pipeline

```python
from webnexus.contracts import SourceConfig, SourceType
from webnexus.integration import attach_pipeline
from webnexus.pipeline import PipelineService, PostgresSink, build_default_registry
from webnexus.storage import create_db_engine, create_session_factory

source = SourceConfig(
    id=source_uuid,
    name="Example News",
    base_url="https://example.com",
    source_type=SourceType.NEWS,
    extractor="news_article",
)
service = PipelineService(PostgresSink(create_session_factory(create_db_engine())))
attach_pipeline(engine, service, source=source)

await engine.add_seed_url("https://example.com/feed.xml", source_id=str(source.id))
await engine.run()
```

For multiple sources, pass `source_resolver=lambda source_id: load_source(source_id)`
instead of `source`. Events can be forwarded to Person C with
`event_sink=callback`; A's event types map to the shared `CrawlEvent` model.

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
| B | `src/webnexus/pipeline/`, `src/webnexus/storage/`, `src/webnexus/contracts/`, `src/webnexus/integration/` |
| C | `api/`, `web/`, orchestration, dashboard, ops |

Contracts in `src/webnexus/contracts/` and `crawler/models.py` require team
agreement before changes.
