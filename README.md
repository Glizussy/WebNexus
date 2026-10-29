# WebNexus

Scalable multi-source web crawler: fetch, normalize, deduplicate, store, and
report on data from news sites, blogs, e-commerce, research repositories, and
public government sites.

## Current status

Shared **data contracts** (`contracts/`, v1.0.0) and the **PostgreSQL schema**
(`storage/models.py` + initial Alembic migration) are in place. Pipeline
extractors, dedup sink, crawler, and dashboard are still to be built.

## Layout

```
src/webnexus/contracts/   versioned Pydantic contracts + hashing helpers (shared)
src/webnexus/storage/     SQLAlchemy models + engine/session helpers (B)
migrations/               Alembic migrations
docs/contracts.md         handoff rules + JSON examples
docs/schema.md            table reference, dedup and incremental design
tests/                    contract + schema tests
```

Planned: `src/webnexus/crawler/` (A), `src/webnexus/pipeline/` (B),
`src/webnexus/api/` + `web/` (C).

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

The database listens on `localhost:5433`; `DATABASE_URL` is read from the
environment (`.env` is loaded by Alembic).

## Ownership

| Person | Area |
|---|---|
| A | `src/webnexus/crawler/` — fetch, robots.txt, rate limits, frontier, scheduler |
| B | `src/webnexus/pipeline/`, `src/webnexus/storage/` — extract, normalize, dedup, persist |
| C | `src/webnexus/api/`, `web/` — orchestration, dashboard, ops |

Contracts in `src/webnexus/contracts/` are shared and require team agreement to
change.
