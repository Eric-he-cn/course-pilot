from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.settings import Settings
from app.store import SQLiteStore
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.api import KnowledgeFeatureDisabledError
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker


class KnowledgeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        data_dir = Path(self.temp_dir.name) / "data"
        self.settings = Settings(
            data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
            text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
            chunk_size=32, chunk_overlap=8, top_k_results=6,
            material_max_bytes=10 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
        )
        self.store = SQLiteStore(self.settings.database_path)
        self.store.migrate()
        self.courses = CourseService(CourseRepository(self.store))
        self.math = self.courses.create_course(name="数学")
        self.physics = self.courses.create_course(name="物理")
        self.wiki_enabled = False
        self.service = KnowledgeService(
            repository=KnowledgeRepository(self.store), settings=self.settings,
            wiki_is_enabled=lambda _course_id: self.wiki_enabled,
        )
        self.worker = KnowledgeJobWorker(self.service, workers=1, queue_capacity=4)
        self.worker.start()

    def tearDown(self) -> None:
        self.worker.shutdown()
        self.temp_dir.cleanup()

    def wait_for_terminal_job(self, job_id: str):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = self.service.get_job(job_id=job_id)
            if job and job.status in {"completed", "failed"}:
                return job
            time.sleep(0.01)
        self.fail(f"job {job_id} did not reach a terminal state")

    def enqueue_and_wait(self, job_id: str):
        self.assertTrue(self.worker.submit(job_id))
        return self.wait_for_terminal_job(job_id)

    def test_index_persists_job_and_course_scoped_retrieval(self) -> None:
        material = self.service.upload_material(
            course_id=self.math.id, filename="calculus.md", mime_type="text/markdown",
            content="链式法则：复合函数求导，先对外层求导，再乘以内层导数。".encode(),
        )
        queued = self.service.enqueue_index(material_id=material.id)
        self.assertIn(queued.status, {"queued", "running"})
        job = self.enqueue_and_wait(queued.id)

        self.assertEqual((job.status, job.stage, job.progress, job.retrieval_backend), ("completed", "completed", 100, "sqlite_fts"))
        self.assertEqual(self.service.list_materials(course_id=self.math.id)[0].index_status, "indexed")
        hits = self.service.search_course(course_id=self.math.id, query="链式法则")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].citation.document, "calculus.md")
        self.assertEqual(hits[0].citation.material_id, material.id)
        natural_query_hits = self.service.search_course(course_id=self.math.id, query="高等数学 II 的链式法则怎么用？")
        self.assertEqual(len(natural_query_hits), 1)
        self.assertIn("链式法则", natural_query_hits[0].content)
        self.assertEqual(self.service.search_course(course_id=self.physics.id, query="链式法则"), [])

    def test_wiki_requires_explicit_course_flag_and_keeps_rag_independent(self) -> None:
        material = self.service.upload_material(
            course_id=self.math.id, filename="notes.txt", mime_type="text/plain", content="极限是微积分的基础。".encode(),
        )
        self.enqueue_and_wait(self.service.enqueue_index(material_id=material.id).id)
        with self.assertRaises(KnowledgeFeatureDisabledError):
            self.service.enqueue_wiki_build(material_id=material.id)
        self.assertEqual(len(self.service.search_course(course_id=self.math.id, query="极限")), 1)

        self.wiki_enabled = True
        job = self.enqueue_and_wait(self.service.enqueue_wiki_build(material_id=material.id).id)
        self.assertEqual((job.type, job.status, job.stage), ("wiki", "completed", "wiki_completed"))
        self.assertTrue((self.settings.data_dir / "wiki" / self.math.id / f"{material.id}.md").is_file())

    def test_invalid_pdf_is_a_failed_job_not_a_crash(self) -> None:
        material = self.service.upload_material(
            course_id=self.math.id, filename="scan.pdf", mime_type="application/pdf", content=b"%PDF-1.4 no text operators",
        )
        job = self.enqueue_and_wait(self.service.enqueue_index(material_id=material.id).id)
        self.assertEqual((job.status, job.stage), ("failed", "failed"))
        self.assertTrue(job.error_message)
        self.assertEqual(self.service.list_materials(course_id=self.math.id)[0].index_status, "failed")

    def test_health_reports_only_knowledge_dependencies(self) -> None:
        health = self.service.health()
        self.assertEqual(health["database"]["ok"], True)
        self.assertGreaterEqual(health["database"]["migration_version"], 2)
        self.assertEqual(health["rag"], {"ok": True, "backend": "sqlite_fts_fallback"})
        self.assertNotIn("llm", health)

    def test_upload_limit_comes_from_settings(self) -> None:
        limited = Settings(
            data_dir=self.settings.data_dir, database_path=self.settings.database_path, uploads_dir=self.settings.uploads_dir,
            text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
            chunk_size=32, chunk_overlap=8, top_k_results=6,
            material_max_bytes=2 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
        )
        service = KnowledgeService(repository=KnowledgeRepository(self.store), settings=limited)
        with self.assertRaisesRegex(ValueError, "2 MiB"):
            service.upload_material(course_id=self.math.id, filename="large.md", mime_type="text/markdown", content=b"x" * (2 * 1024 * 1024 + 1))

    def test_restart_recovers_queued_and_marks_running_failed(self) -> None:
        material = self.service.upload_material(course_id=self.math.id, filename="queued.md", mime_type="text/markdown", content="极限定义".encode())
        queued = self.service.enqueue_index(material_id=material.id)
        running = self.service.enqueue_index(material_id=material.id)
        self.service._repository.claim_queued_job(running.id)
        self.worker.shutdown()
        restarted = KnowledgeJobWorker(self.service, workers=1, queue_capacity=4)
        restarted.start()
        try:
            recovered = self.wait_for_terminal_job(queued.id)
            interrupted = self.service.get_job(job_id=running.id)
            self.assertEqual(recovered.status, "completed")
            self.assertEqual(interrupted.status, "failed")
            self.assertIn("重启", interrupted.error_message or "")
        finally:
            restarted.shutdown()


if __name__ == "__main__":
    unittest.main()
