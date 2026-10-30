"""Configuration and monitoring endpoints for the control plane.

Sources, keywords and schedules use the shared contract models
(``webnexus.contracts.models``) and are held in memory for now. Swapping the
in-memory dictionaries for the PostgreSQL tables (``sources``, ``keywords``)
is a later step that needs a running database.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException

from webnexus.contracts.models import CrawlRunConfig, KeywordRule, SourceConfig

ERROR_EVENT_TYPES = {"failure", "robots_blocked", "rate_limited", "retry"}


def _host(url: str) -> str | None:
    return (urlparse(url).hostname or "").lower() or None


def _source_domains(source: SourceConfig) -> set[str]:
    domains = {d.lower() for d in source.allowed_domains}
    base = _host(source.base_url)
    if base:
        domains.add(base)
    return domains


def create_router(jobs: dict[str, Any]) -> APIRouter:
    router = APIRouter(prefix="/api")
    sources: dict[UUID, SourceConfig] = {}
    keywords: dict[str, KeywordRule] = {}
    schedules: dict[UUID, CrawlRunConfig] = {}

    # ----------------------------------------------------------------- sources
    @router.post("/sources", status_code=201)
    async def create_source(source: SourceConfig):
        if any(s.name.lower() == source.name.lower() for s in sources.values()):
            raise HTTPException(status_code=409, detail="A source with this name already exists")
        stored = source.model_copy(update={"id": uuid4()})
        sources[stored.id] = stored
        return stored

    @router.get("/sources")
    async def list_sources():
        return {"sources": list(sources.values())}

    @router.get("/sources/health")
    async def source_health():
        report = []
        for source in sources.values():
            domains = _source_domains(source)
            related = [
                job for job in jobs.values()
                if any(_host(url) in domains for url in job.urls)
            ]
            failed = sum(1 for job in related if job.status == "failed")
            last = related[-1] if related else None
            if not related:
                health = "unknown"
            elif last.status == "failed":
                health = "unhealthy"
            elif failed:
                health = "degraded"
            else:
                health = "healthy"
            report.append({
                "source_id": source.id,
                "name": source.name,
                "health": health,
                "runs": len(related),
                "failed_runs": failed,
                "last_status": last.status if last else None,
                "last_run_at": (last.finished_at or last.started_at) if last else None,
            })
        return {"sources": report}

    @router.get("/sources/{source_id}")
    async def get_source(source_id: UUID):
        if source_id not in sources:
            raise HTTPException(status_code=404, detail="Source not found")
        return sources[source_id]

    @router.put("/sources/{source_id}")
    async def update_source(source_id: UUID, source: SourceConfig):
        if source_id not in sources:
            raise HTTPException(status_code=404, detail="Source not found")
        stored = source.model_copy(update={"id": source_id})
        sources[source_id] = stored
        return stored

    @router.delete("/sources/{source_id}", status_code=204)
    async def delete_source(source_id: UUID):
        if sources.pop(source_id, None) is None:
            raise HTTPException(status_code=404, detail="Source not found")

    @router.post("/sources/{source_id}/run", status_code=202)
    async def run_source(source_id: UUID):
        """Start a crawl from the source's seed URLs (falls back to its base URL)."""
        from api.main import start_job  # imported lazily to avoid a circular import

        source = sources.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        return start_job(source.seed_urls or [source.base_url]).to_dict()

    # ---------------------------------------------------------------- keywords
    @router.post("/keywords", status_code=201)
    async def create_keyword(rule: KeywordRule):
        key = rule.term.lower()
        if key in keywords:
            raise HTTPException(status_code=409, detail="Keyword already exists")
        keywords[key] = rule
        return rule

    @router.get("/keywords")
    async def list_keywords():
        return {"keywords": list(keywords.values())}

    @router.delete("/keywords/{term}", status_code=204)
    async def delete_keyword(term: str):
        if keywords.pop(term.strip().lower(), None) is None:
            raise HTTPException(status_code=404, detail="Keyword not found")

    # --------------------------------------------------------------- schedules
    @router.post("/schedules", status_code=201)
    async def create_schedule(config: CrawlRunConfig):
        cron = (config.schedule_cron or "").split()
        if len(cron) != 5:
            raise HTTPException(
                status_code=422,
                detail="schedule_cron must be a 5-field cron expression",
            )
        schedule_id = uuid4()
        schedules[schedule_id] = config
        return {"id": schedule_id, **config.model_dump(mode="json")}

    @router.get("/schedules")
    async def list_schedules():
        return {
            "schedules": [
                {"id": sid, **cfg.model_dump(mode="json")} for sid, cfg in schedules.items()
            ]
        }

    @router.delete("/schedules/{schedule_id}", status_code=204)
    async def delete_schedule(schedule_id: UUID):
        if schedules.pop(schedule_id, None) is None:
            raise HTTPException(status_code=404, detail="Schedule not found")

    # ------------------------------------------------------------------ errors
    @router.get("/errors")
    async def list_errors(limit: int = 100):
        limit = max(1, min(limit, 500))
        errors: list[dict[str, Any]] = []
        for job in jobs.values():
            if job.error:
                errors.append({
                    "job_id": job.id,
                    "kind": "job_failed",
                    "url": None,
                    "message": job.error,
                    "timestamp_utc": job.finished_at,
                })
            for event in job.events:
                if event.get("event_type") in ERROR_EVENT_TYPES:
                    errors.append({
                        "job_id": job.id,
                        "kind": event["event_type"],
                        "url": event.get("url"),
                        "message": event.get("message"),
                        "timestamp_utc": event.get("timestamp_utc"),
                    })
        errors.sort(key=lambda e: e["timestamp_utc"] or "")
        return {"errors": errors[-limit:]}

    return router
