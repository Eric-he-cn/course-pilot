from __future__ import annotations

import io
import time
from dataclasses import dataclass, replace

import pytest

from core.settings import Settings
from core.store import SQLiteStore
from modules.courses.models import Course
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.api import KnowledgeFeatureDisabledError
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker


def _pdf_with_pages(page_texts: list[str]) -> bytes:
    """Build a minimal PDF with one text content stream per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(page_texts)))
    font_ref = 3 + len(page_texts) * 2
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode())
    for index, text in enumerate(page_texts):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + index * 2} 0 R "
            f"/Resources << /Font << /F1 {font_ref} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >> stream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj ".encode() + body + b" endobj\n")
    xref_at = buffer.tell()
    buffer.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode())
    return buffer.getvalue()


@dataclass
class KnowledgeEnv:
    settings: Settings
    store: SQLiteStore
    service: KnowledgeService
    worker: KnowledgeJobWorker
    math: Course
    physics: Course
    wiki_enabled: bool = False

    def wait_terminal(self, job_id: str):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            job = self.service.get_job(job_id=job_id)
            if job and job.status in {"completed", "failed"}:
                return job
            time.sleep(0.01)
        pytest.fail(f"job {job_id} did not reach a terminal state")

    def run_job(self, job_id: str):
        assert self.worker.submit(job_id)
        return self.wait_terminal(job_id)


@pytest.fixture
def env(tmp_path):
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=32, chunk_overlap=8, top_k_results=6,
        material_max_bytes=10 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    courses = CourseService(CourseRepository(store))
    holder: list[KnowledgeEnv] = []
    service = KnowledgeService(
        repository=KnowledgeRepository(store), settings=settings,
        wiki_is_enabled=lambda _course_id: holder[0].wiki_enabled,
    )
    worker = KnowledgeJobWorker(service, workers=1, queue_capacity=4)
    worker.start()
    holder.append(KnowledgeEnv(settings, store, service, worker, courses.create_course(name="数学"), courses.create_course(name="物理")))
    yield holder[0]
    worker.shutdown()


def test_index_persists_job_and_course_scoped_retrieval(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="calculus.md", mime_type="text/markdown",
        content="链式法则：复合函数求导，先对外层求导，再乘以内层导数。".encode(),
    )
    queued = env.service.enqueue_index(material_id=material.id)
    assert queued.status in {"queued", "running"}
    job = env.run_job(queued.id)

    assert (job.status, job.stage, job.progress, job.retrieval_backend) == ("completed", "completed", 100, "sqlite_fts")
    assert env.service.list_materials(course_id=env.math.id)[0].index_status == "indexed"
    hits = env.service.search_course(course_id=env.math.id, query="链式法则")
    assert len(hits) == 1
    assert hits[0].citation.document == "calculus.md"
    assert hits[0].citation.material_id == material.id
    natural_query_hits = env.service.search_course(course_id=env.math.id, query="高等数学 II 的链式法则怎么用？")
    assert len(natural_query_hits) == 1
    assert "链式法则" in natural_query_hits[0].content
    assert env.service.search_course(course_id=env.physics.id, query="链式法则") == []


def test_wiki_requires_explicit_course_flag_and_keeps_rag_independent(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="notes.txt", mime_type="text/plain", content="极限是微积分的基础。".encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    with pytest.raises(KnowledgeFeatureDisabledError):
        env.service.enqueue_wiki_build(material_id=material.id)
    assert len(env.service.search_course(course_id=env.math.id, query="极限")) == 1

    env.wiki_enabled = True
    job = env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    assert (job.type, job.status, job.stage) == ("wiki", "completed", "wiki_completed")
    assert (env.settings.data_dir / "wiki" / env.math.id / f"{material.id}.md").is_file()


def test_pdf_chunks_keep_their_page_numbers_in_citations(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="rules.pdf", mime_type="application/pdf",
        content=_pdf_with_pages(["The chain rule lives on page one", "The product rule lives on page two"]),
    )
    job = env.run_job(env.service.enqueue_index(material_id=material.id).id)
    assert job.status == "completed"

    chain = env.service.search_course(course_id=env.math.id, query="chain rule")
    assert chain[0].citation.page == 1
    product = env.service.search_course(course_id=env.math.id, query="product rule")
    assert product[0].citation.page == 2


def test_invalid_pdf_is_a_failed_job_not_a_crash(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="scan.pdf", mime_type="application/pdf", content=b"%PDF-1.4 no text operators",
    )
    job = env.run_job(env.service.enqueue_index(material_id=material.id).id)
    assert (job.status, job.stage) == ("failed", "failed")
    assert job.error_message
    assert env.service.list_materials(course_id=env.math.id)[0].index_status == "failed"


def test_health_reports_only_knowledge_dependencies(env):
    health = env.service.health()
    assert health["database"]["ok"] is True
    assert health["database"]["migration_version"] >= 2
    assert health["rag"] == {"ok": True, "backend": "sqlite_fts_fallback"}
    assert "llm" not in health


def test_upload_limit_comes_from_settings(env):
    limited = replace(env.settings, material_max_bytes=2 * 1024 * 1024)
    service = KnowledgeService(repository=KnowledgeRepository(env.store), settings=limited)
    with pytest.raises(ValueError, match="2 MiB"):
        service.upload_material(course_id=env.math.id, filename="large.md", mime_type="text/markdown", content=b"x" * (2 * 1024 * 1024 + 1))


def test_restart_recovers_queued_and_marks_running_failed(env):
    material = env.service.upload_material(course_id=env.math.id, filename="queued.md", mime_type="text/markdown", content="极限定义".encode())
    queued = env.service.enqueue_index(material_id=material.id)
    running = env.service.enqueue_index(material_id=material.id)
    env.service._repository.claim_queued_job(running.id)
    env.worker.shutdown()
    restarted = KnowledgeJobWorker(env.service, workers=1, queue_capacity=4)
    restarted.start()
    try:
        recovered = env.wait_terminal(queued.id)
        interrupted = env.service.get_job(job_id=running.id)
        assert recovered.status == "completed"
        assert interrupted.status == "failed"
        assert "重启" in (interrupted.error_message or "")
    finally:
        restarted.shutdown()
