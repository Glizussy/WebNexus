"""Tests for the sources, keywords, schedules and errors endpoints."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.main import CrawlJob, app, jobs  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

SOURCE = {
    "name": "Quotes",
    "base_url": "https://quotes.toscrape.com",
    "source_type": "blog",
    "seed_urls": ["https://quotes.toscrape.com/"],
    "allowed_domains": ["quotes.toscrape.com"],
}


def test_source_lifecycle():
    created = client.post("/api/sources", json={**SOURCE, "name": "Lifecycle"})
    assert created.status_code == 201
    source_id = created.json()["id"]
    assert client.get(f"/api/sources/{source_id}").status_code == 200

    updated = client.put(f"/api/sources/{source_id}", json={**SOURCE, "name": "Lifecycle 2"})
    assert updated.status_code == 200
    assert updated.json()["id"] == source_id
    assert updated.json()["name"] == "Lifecycle 2"

    assert client.delete(f"/api/sources/{source_id}").status_code == 204
    assert client.get(f"/api/sources/{source_id}").status_code == 404


def test_source_rejects_unknown_fields_and_duplicates():
    assert client.post("/api/sources", json={**SOURCE, "bogus": 1}).status_code == 422
    first = client.post("/api/sources", json={**SOURCE, "name": "Dup"})
    assert first.status_code == 201
    assert client.post("/api/sources", json={**SOURCE, "name": "dup"}).status_code == 409
    client.delete(f"/api/sources/{first.json()['id']}")


def test_unknown_source_returns_404():
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/sources/{missing}").status_code == 404
    assert client.delete(f"/api/sources/{missing}").status_code == 404
    assert client.post(f"/api/sources/{missing}/run").status_code == 404


def test_source_health_reflects_runs():
    created = client.post("/api/sources", json={**SOURCE, "name": "Health"}).json()
    failed = CrawlJob(["https://quotes.toscrape.com/"])
    failed.status = "failed"
    failed.error = "boom"
    failed.finished_at = "2026-10-29T12:00:00+00:00"
    jobs[failed.id] = failed
    try:
        report = client.get("/api/sources/health").json()["sources"]
        entry = next(s for s in report if s["source_id"] == created["id"])
        assert entry["health"] == "unhealthy"
        assert entry["failed_runs"] == 1
    finally:
        jobs.pop(failed.id, None)
        client.delete(f"/api/sources/{created['id']}")


def test_keywords_lifecycle():
    assert client.post("/api/keywords", json={"term": "Research"}).status_code == 201
    assert client.post("/api/keywords", json={"term": "research"}).status_code == 409
    assert client.post("/api/keywords", json={"term": "   "}).status_code == 422
    terms = [k["term"] for k in client.get("/api/keywords").json()["keywords"]]
    assert "Research" in terms
    assert client.delete("/api/keywords/research").status_code == 204
    assert client.delete("/api/keywords/research").status_code == 404


def test_schedules_require_cron_expression():
    assert client.post("/api/schedules", json={}).status_code == 422
    assert client.post("/api/schedules", json={"schedule_cron": "* *"}).status_code == 422
    created = client.post("/api/schedules", json={"schedule_cron": "0 6 * * *"})
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    listed = client.get("/api/schedules").json()["schedules"]
    assert any(s["id"] == schedule_id for s in listed)
    assert client.delete(f"/api/schedules/{schedule_id}").status_code == 204
    assert client.delete(f"/api/schedules/{schedule_id}").status_code == 404


def test_errors_lists_failed_jobs_and_failure_events():
    job = CrawlJob(["https://example.com/"])
    job.status = "failed"
    job.error = "No seed URLs could be scheduled."
    job.finished_at = "2026-10-29T12:00:00+00:00"
    job.events.append({
        "event_type": "failure",
        "url": "https://example.com/x",
        "message": "timeout",
        "timestamp_utc": "2026-10-29T11:59:00+00:00",
    })
    job.events.append({"event_type": "fetch_success", "url": "https://example.com/"})
    jobs[job.id] = job
    try:
        errors = [e for e in client.get("/api/errors").json()["errors"] if e["job_id"] == job.id]
        assert {e["kind"] for e in errors} == {"job_failed", "failure"}
    finally:
        jobs.pop(job.id, None)


def test_run_source_starts_a_job_from_its_base_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    created = client.post(
        "/api/sources", json={**SOURCE, "name": "Runner", "base_url": "notaurl", "seed_urls": []}
    ).json()
    try:
        response = client.post(f"/api/sources/{created['id']}/run")
        assert response.status_code == 202
        assert response.json()["urls"] == ["notaurl"]
        jobs.pop(response.json()["id"], None)
    finally:
        client.delete(f"/api/sources/{created['id']}")
