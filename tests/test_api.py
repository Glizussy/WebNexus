import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app, extract_allowed_domains  # noqa: E402

client = TestClient(app)


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_run_rejects_empty_url_list():
    response = client.post("/api/runs", json={"urls": []})
    assert response.status_code == 422


def test_create_run_rejects_missing_body():
    response = client.post("/api/runs", json={})
    assert response.status_code == 422


def test_unknown_run_returns_404():
    assert client.get("/api/runs/does-not-exist").status_code == 404


def test_control_actions_on_unknown_run_return_404():
    for action in ("stop", "pause", "resume"):
        response = client.post(f"/api/runs/does-not-exist/{action}")
        assert response.status_code == 404, action


def test_stats_has_expected_keys():
    data = client.get("/api/stats").json()
    for key in ("total_runs", "queued", "running", "paused", "completed", "failed", "stopped"):
        assert key in data


def test_logs_limit_is_clamped():
    response = client.get("/api/logs?limit=100000")
    assert response.status_code == 200
    assert len(response.json()["logs"]) <= 500


def test_metrics_format():
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert "webnexus_pages_crawled" in response.text
    assert response.text.endswith("\n")


def test_extract_allowed_domains():
    domains = extract_allowed_domains(
        ["https://Quotes.toscrape.com/page/1", "http://example.com:8080/", "notaurl"]
    )
    assert domains == {"quotes.toscrape.com", "example.com"}


def test_dashboard_is_served():
    response = client.get("/dashboard/")
    assert response.status_code == 200
    assert "WebNexus" in response.text