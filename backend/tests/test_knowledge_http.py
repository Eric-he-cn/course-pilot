from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

class KnowledgeHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        from app.main import create_app
        from core.settings import Settings

        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        settings = Settings(
            data_dir=root / "data", database_path=root / "data" / "coursepilot.db", uploads_dir=root / "data" / "materials",
            text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
            chunk_size=60, chunk_overlap=10, top_k_results=6,
            material_max_bytes=10 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
        )
        self.client = TestClient(create_app(settings=settings))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def test_course_scoped_material_index_search_wiki_and_health(self) -> None:
        calculus = self.client.post("/api/v2/courses", json={"name": "高等数学"}).json()
        physics = self.client.post("/api/v2/courses", json={"name": "大学物理"}).json()
        upload = self.client.post(
            f"/api/v2/courses/{calculus['id']}/materials",
            files={"file": ("calculus.md", "链式法则：先求外层导数，再乘内层导数。", "text/markdown")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        material = upload.json()
        job = self.client.post(f"/api/v2/materials/{material['id']}/index")
        self.assertEqual(job.status_code, 200, job.text)
        self.assertIn(job.json()["status"], {"queued", "running", "completed"})
        job = self.poll_job(job.json()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["retrieval_backend"], "sqlite_fts")

        results = self.client.post(f"/api/v2/courses/{calculus['id']}/knowledge/search", json={"query": "链式法则"})
        self.assertEqual(results.status_code, 200, results.text)
        self.assertEqual(results.json()[0]["material_id"], material["id"])
        self.assertEqual(self.client.post(f"/api/v2/courses/{physics['id']}/knowledge/search", json={"query": "链式法则"}).json(), [])

        self.assertEqual(self.client.post(f"/api/v2/materials/{material['id']}/wiki").status_code, 409)
        self.assertEqual(self.client.patch(f"/api/v2/courses/{calculus['id']}", json={"wiki_enabled": True}).status_code, 200)
        wiki = self.client.post(f"/api/v2/materials/{material['id']}/wiki")
        self.assertEqual(wiki.status_code, 200)
        self.assertEqual(self.poll_job(wiki.json()["id"])["stage"], "wiki_completed")

        health = self.client.get("/api/v2/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["llm"]["mode"], "demo_fallback")
        self.assertEqual(health.json()["llm"]["enabled"], False)
        self.assertNotIn("api_key", health.json()["llm"])

    def poll_job(self, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/v2/jobs/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job["status"] in {"completed", "failed"}:
                return job
            time.sleep(0.01)
        self.fail(f"job {job_id} did not reach a terminal state")


if __name__ == "__main__":
    unittest.main()
