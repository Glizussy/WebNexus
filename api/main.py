"""WebNexus Control Plane API."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from crawler import CrawlerEngine
from crawler.config import CrawlerConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webnexus.api")

app = FastAPI(
    title="WebNexus API",
    description="Control plane for the WebNexus web crawler.",
    version="0.1.0",
)


class CreateRunRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)


class CrawlJob:
    def __init__(self, urls: list[str]) -> None:
        self.id = str(uuid4())
        self.urls = urls
        self.status = "queued"
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.error: str | None = None
        self.events: list[dict[str, Any]] = []
        self.engine: CrawlerEngine | None = None
        self.task: asyncio.Task | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "urls": self.urls,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "metrics": self.engine.get_metrics() if self.engine else {},
            "events": self.events[-100:],
        }


jobs: dict[str, CrawlJob] = {}


def extract_allowed_domains(urls: list[str]) -> set[str]:
    """Derive the set of in-scope hostnames from the seed URLs."""
    domains: set[str] = set()
    for url in urls:
        hostname = urlparse(url).hostname
        if hostname:
            domains.add(hostname.lower())
    return domains

async def execute_job(job: CrawlJob) -> None:
    state_dir = Path("data") / "runs"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = CrawlerConfig(
        allowed_domains=extract_allowed_domains(job.urls),
        state_db_path=str(state_dir / f"{job.id}.db"),
    )
    engine = CrawlerEngine(config=config)
    job.engine = engine

    def record_event(event) -> None:
        job.events.append(event.to_dict())
        if len(job.events) > 1000:
            del job.events[:-1000]

    engine.add_event_listener(record_event)

    try:
        job.status = "running"
        job.started_at = datetime.now(timezone.utc).isoformat()

        added_count = await engine.add_seed_urls(job.urls)

        if added_count == 0:
            raise RuntimeError("No seed URLs could be scheduled.")

        await engine.run()

        if job.status != "stopping":
            job.status = "completed"

    except asyncio.CancelledError:
        job.status = "stopped"
        raise
    except Exception as exc:
        logger.exception("Crawl job %s failed", job.id)
        job.error = str(exc)
        job.status = "failed"
    finally:
        if job.status == "stopping":
            job.status = "stopped"
            job.finished_at = datetime.now(timezone.utc).isoformat()
@app.get("/")
async def home():
    return {
        "message": "Welcome to WebNexus",
        "description": "Web Crawler Control Plane API",
        "docs": "/docs",
        "health": "/api/health"
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "WebNexus API"}


@app.post("/api/runs", status_code=202)
async def create_run(request: CreateRunRequest):
    job = CrawlJob(request.urls)
    jobs[job.id] = job
    job.task = asyncio.create_task(execute_job(job))
    return job.to_dict()


@app.get("/api/runs")
async def list_runs():
    return {"runs": [job.to_dict() for job in jobs.values()]}


@app.get("/api/runs/{job_id}")
async def get_run(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Crawl run not found")
    return job.to_dict()


@app.post("/api/runs/{job_id}/stop")
async def stop_run(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Crawl run not found")

    if job.task is None or job.task.done():
        raise HTTPException(status_code=409, detail="Crawl run is not active")

    job.status = "stopping"
    if job.engine is not None:
        await job.engine.stop()

    return {"id": job.id, "status": job.status}


@app.post("/api/runs/{job_id}/pause")
async def pause_run(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Crawl run not found")
    if job.status != "running" or job.engine is None:
        raise HTTPException(status_code=409, detail="Crawl run is not running")

    job.engine.pause()
    job.status = "paused"
    return {"id": job.id, "status": job.status}


@app.post("/api/runs/{job_id}/resume")
async def resume_run(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Crawl run not found")
    if job.status != "paused" or job.engine is None:
        raise HTTPException(status_code=409, detail="Crawl run is not paused")

    job.engine.resume()
    job.status = "running"
    return {"id": job.id, "status": job.status}


@app.get("/api/stats")
async def get_stats():
    totals = {
        "total_runs": len(jobs),
        "queued": 0,
        "running": 0,
        "paused": 0,
        "completed": 0,
        "failed": 0,
        "stopped": 0,
    }

    for job in jobs.values():
        status = job.status
        if status in totals:
            totals[status] += 1

    return totals


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    limit = max(1, min(limit, 500))
    events = [
        {"job_id": job.id, **event}
        for job in jobs.values()
        for event in job.events
    ]
    return {"logs": events[-limit:]}

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/dashboard", StaticFiles(directory=WEB_DIR, html=True), name="dashboard")