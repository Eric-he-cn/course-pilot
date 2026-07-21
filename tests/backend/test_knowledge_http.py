from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import Settings


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "data" / "coursepilot.db", uploads_dir=tmp_path / "data" / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=60, chunk_overlap=10, top_k_results=6,
        material_max_bytes=10 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _poll_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"job {job_id} did not reach a terminal state")


def test_course_scoped_material_index_search_wiki_and_health(client):
    calculus = client.post("/api/v2/courses", json={"name": "高等数学"}).json()
    physics = client.post("/api/v2/courses", json={"name": "大学物理"}).json()
    upload = client.post(
        f"/api/v2/courses/{calculus['id']}/materials",
        files={"file": ("calculus.md", "链式法则：先求外层导数，再乘内层导数。", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text
    material = upload.json()
    job = client.post(f"/api/v2/materials/{material['id']}/index")
    assert job.status_code == 200, job.text
    assert job.json()["status"] in {"queued", "running", "completed"}
    job = _poll_job(client, job.json()["id"])
    assert job["status"] == "completed"
    assert job["retrieval_backend"] == "sqlite_fts"

    results = client.post(f"/api/v2/courses/{calculus['id']}/knowledge/search", json={"query": "链式法则"})
    assert results.status_code == 200, results.text
    assert results.json()[0]["material_id"] == material["id"]
    assert client.post(f"/api/v2/courses/{physics['id']}/knowledge/search", json={"query": "链式法则"}).json() == []

    assert client.post(f"/api/v2/materials/{material['id']}/wiki").status_code == 409
    assert client.patch(f"/api/v2/courses/{calculus['id']}", json={"wiki_enabled": True}).status_code == 200
    wiki = client.post(f"/api/v2/materials/{material['id']}/wiki")
    assert wiki.status_code == 200
    assert _poll_job(client, wiki.json()["id"])["stage"] == "wiki_completed"

    health = client.get("/api/v2/health")
    assert health.status_code == 200
    assert health.json()["llm"]["mode"] == "demo_fallback"
    assert health.json()["llm"]["enabled"] is False
    assert "api_key" not in health.json()["llm"]
