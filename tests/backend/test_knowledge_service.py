from __future__ import annotations

import io
import struct
import time
from dataclasses import dataclass, replace

import pytest

from contracts.knowledge import ResolvedKnowledgeScope
from contracts.llm import ChatFinal
from core.settings import Settings
from core.store import SQLiteStore
from modules.courses.models import Course
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.api import KnowledgeFeatureDisabledError
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.wiki import HANDWRITTEN_MARKER, WikiStore
from modules.knowledge.worker import KnowledgeJobWorker


class StubResponder:
    """Wiki 每页一次模型调用。这里只要一段确定的正文，别让测试依赖真模型。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, *, messages, tools=()):
        self.prompts.append(messages[-1].content)
        yield ChatFinal(text="一句话定义：这是生成的概念页。[p.1]", finish_reason="stop",
                        provider="stub", model="stub", mode="stub")


WIKI_MATERIAL = """# 极限

极限描述的是函数在某一点附近的趋势。

# 连续性

连续性建立在极限之上。
"""


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
    responder: object | None = None

    def wait_terminal(self, job_id: str):
        # 别卡得太紧：索引要读 PDF、切块、抽概念，机器上有别的负载时 2 秒会超时，
        # 表现成一条看不出原因的偶发失败。真坏了任务会很快落到 failed，不靠这个超时发现。
        deadline = time.monotonic() + 20
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
    responder = StubResponder()
    service = KnowledgeService(
        repository=KnowledgeRepository(store), settings=settings,
        wiki_is_enabled=lambda _course_id: holder[0].wiki_enabled,
        wiki_store=WikiStore(settings.data_dir), responder=responder,
    )
    worker = KnowledgeJobWorker(service, workers=1, queue_capacity=4)
    worker.start()
    holder.append(KnowledgeEnv(settings, store, service, worker, courses.create_course(name="数学"), courses.create_course(name="物理"), responder=responder))
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
        course_id=env.math.id, filename="notes.md", mime_type="text/markdown", content=WIKI_MATERIAL.encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    with pytest.raises(KnowledgeFeatureDisabledError):
        env.service.enqueue_wiki_build(material_id=material.id)
    # 两块都含「极限」：标题那块，以及句中提到它的「连续性建立在极限之上」。
    # 这里要的是「Wiki 关掉不影响 RAG」，检索本身照常返回全部命中。
    assert len(env.service.search_course(course_id=env.math.id, query="极限")) == 2

    env.wiki_enabled = True
    job = env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    assert (job.type, job.status, job.stage) == ("wiki", "completed", "wiki_completed")

    pages = env.service.wiki_pages(course_id=env.math.id)
    assert pages, "启用后应该真的写出概念页"
    # 首页排在最前，它读的是别的页；教材出处要到读原文的那一页上看
    assert pages[0]["concept_id"] == "index"
    content = env.service.wiki_page(course_id=env.math.id, concept_id=pages[1]["concept_id"])
    # frontmatter 要能追溯：概念 id、证据指纹、提示词版本
    assert "concept_id:" in content and "source_hash:" in content and "prompt_version: wiki-v2" in content
    assert "source_refs:" in content and "notes.md" in content


def test_wiki_only_feeds_the_model_retrieved_material(env):
    """写页的证据必须来自检索，不能让模型拿通用知识补。"""
    material = env.service.upload_material(
        course_id=env.math.id, filename="notes.md", mime_type="text/markdown", content=WIKI_MATERIAL.encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    env.wiki_enabled = True
    env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    assert env.responder.prompts, "应该调用过模型"
    assert "极限描述的是函数在某一点附近的趋势" in env.responder.prompts[0], "教材原文要进提示词"
    assert "notes.md" in env.responder.prompts[0], "出处也要给模型，它才能标引用"


def test_rebuilding_skips_pages_whose_evidence_did_not_change(env):
    """证据没变就不重写：既省 token，也避免每次生成一个不一样的版本。"""
    material = env.service.upload_material(
        course_id=env.math.id, filename="notes.md", mime_type="text/markdown", content=WIKI_MATERIAL.encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    env.wiki_enabled = True
    env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    first_round = len(env.responder.prompts)
    assert first_round > 0

    job = env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    assert len(env.responder.prompts) == first_round, "第二次不该再调模型"
    assert "written=0" in (job.error_message or "") and "skipped=2" in (job.error_message or "")


def test_rebuilding_keeps_the_handwritten_block(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="notes.md", mime_type="text/markdown", content=WIKI_MATERIAL.encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    env.wiki_enabled = True
    env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    pages = env.service.wiki_pages(course_id=env.math.id)
    concept_id = pages[0]["concept_id"]

    store = WikiStore(env.settings.data_dir)
    path = env.settings.data_dir / "wiki" / env.math.id / f"{concept_id}.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n我自己补的：这里容易和连续性搞混。\n", encoding="utf-8")

    # 换一份证据强制重写，手写区必须留着
    store.write(course_id=env.math.id, concept_id=concept_id, concept_name="极限",
                body="重新生成的正文", source_hash="different", source_refs=["notes.txt #x"], updated_at="now")
    rewritten = store.read(course_id=env.math.id, concept_id=concept_id)
    assert "重新生成的正文" in rewritten
    assert "我自己补的" in rewritten
    assert rewritten.count(HANDWRITTEN_MARKER) == 1


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


class FakeEmbedder:
    """双语关键词特征向量：让"注意力"与 attention 落在同一维度，模拟跨语言语义。"""

    name = "fake-embedder"
    unavailable = False

    def status(self):
        return {"model": self.name, "loaded": True, "error": None}

    @staticmethod
    def _vector(text: str) -> bytes:
        lowered = text.lower()
        return struct.pack(
            "2f",
            1.0 if ("attention" in lowered or "注意力" in lowered) else 0.0,
            1.0 if ("gradient" in lowered or "梯度" in lowered) else 0.0,
        )

    def embed_documents(self, texts):
        if self.unavailable:
            return None
        return [self._vector(text) for text in texts]

    def rank(self, *, query, vectors, top_k):
        query_vector = struct.unpack("2f", self._vector(query))
        scored = [
            (index, sum(a * b for a, b in zip(query_vector, struct.unpack("2f", vector))))
            for index, vector in enumerate(vectors)
        ]
        return sorted([(i, s) for i, s in scored if s > 0], key=lambda x: -x[1])[:top_k]


def test_semantic_leg_matches_cross_language_without_lexical_overlap(env):
    service = KnowledgeService(
        repository=KnowledgeRepository(env.store), settings=env.settings,
        embedder=FakeEmbedder(),
    )
    material = service.upload_material(
        course_id=env.math.id, filename="attention.md", mime_type="text/markdown",
        content=b"The attention mechanism weighs token relevance.",
    )
    job = service.run_job(job_id=service.enqueue_index(material_id=material.id).id)
    assert job.retrieval_backend == "hybrid_bge"

    # 中文语义查询与英文内容零词面交集，只有语义腿能命中。
    hits = service.search_course(course_id=env.math.id, query="注意力机制", limit=6)
    assert hits
    assert hits[0].citation.document == "attention.md"
    assert service.health()["rag"]["backend"] == "hybrid_bge"


def test_embedder_unavailable_degrades_to_lexical_indexing(env):
    broken = FakeEmbedder()
    broken.unavailable = True
    service = KnowledgeService(repository=KnowledgeRepository(env.store), settings=env.settings, embedder=broken)
    material = service.upload_material(
        course_id=env.math.id, filename="plain.md", mime_type="text/markdown", content=b"gradient descent updates weights",
    )
    job = service.run_job(job_id=service.enqueue_index(material_id=material.id).id)
    assert job.status == "completed"
    assert job.retrieval_backend == "sqlite_fts"
    assert service.search_course(course_id=env.math.id, query="gradient descent")


def test_mixed_language_query_matches_english_material(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="dl-notes.md", mime_type="text/markdown",
        content=b"Deep learning uses attention and transformers to model sequences.",
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    hits = env.service.search_course(course_id=env.math.id, query="你有没有Deep Learning相关的英文教材？我上传的")
    assert hits
    assert hits[0].citation.document == "dl-notes.md"
    scope = ResolvedKnowledgeScope(turn_id="turn-x", course_id=env.math.id, resolver_version="v1")
    assert "dl-notes.md" in env.service.material_names(scope=scope)


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


def _indexed_wiki_material(env):
    material = env.service.upload_material(
        course_id=env.math.id, filename="notes.md", mime_type="text/markdown", content=WIKI_MATERIAL.encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    env.wiki_enabled = True
    return material


def _coverage(job) -> dict[str, int]:
    return {key: int(value) for key, _, value
            in (item.partition("=") for item in (job.error_message or "").split()[1:]) if value.isdigit()}


def test_a_restart_during_a_wiki_build_leaves_the_material_alone(env):
    """知识页构建被重启打断：作业记成中断，教材不能跟着降级——它的 chunks 与向量一个没动，
    降了就要整份重索引才解得开。"""
    material = _indexed_wiki_material(env)
    interrupted = env.service.enqueue_wiki_build(material_id=material.id)
    env.service._repository.claim_queued_job(interrupted.id)
    env.worker.shutdown()

    restarted = KnowledgeJobWorker(env.service, workers=1, queue_capacity=4)
    restarted.start()
    try:
        assert env.service.get_job(job_id=interrupted.id).status == "failed"
        assert env.service.list_materials(course_id=env.math.id)[0].index_status == "indexed"
        # 重启之后还能原地再建一次，用户不必先去重新索引。
        again = env.service.enqueue_wiki_build(material_id=material.id)
        assert restarted.submit(again.id)
        assert env.wait_terminal(again.id).status == "completed"
    finally:
        restarted.shutdown()
    assert env.service.wiki_pages(course_id=env.math.id)


def test_a_queued_wiki_build_is_picked_up_after_a_restart(env):
    """排着队还没跑的构建，重启后要有人接着跑。丢掉的话界面上那一行会一直转圈。"""
    material = _indexed_wiki_material(env)
    queued = env.service.enqueue_wiki_build(material_id=material.id)
    env.worker.shutdown()

    restarted = KnowledgeJobWorker(env.service, workers=1, queue_capacity=4)
    restarted.start()
    try:
        recovered = env.wait_terminal(queued.id)
    finally:
        restarted.shutdown()

    assert (recovered.status, recovered.stage) == ("completed", "wiki_completed")
    assert env.service.wiki_pages(course_id=env.math.id)


def test_clicking_build_twice_writes_each_page_once(env):
    """同一份教材连点两次构建。第二次要全部命中已有页——重写一遍既费一轮模型调用，
    也会把同一节换个说法再生成一版。"""
    material = _indexed_wiki_material(env)
    first = env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)
    calls_after_first = len(env.responder.prompts)
    pages_after_first = {page["concept_id"]: page["chars"] for page in env.service.wiki_pages(course_id=env.math.id)}

    second = env.run_job(env.service.enqueue_wiki_build(material_id=material.id).id)

    assert _coverage(first)["written"] > 0 and _coverage(second)["written"] == 0, second.error_message
    assert _coverage(second)["skipped"] == _coverage(first)["written"]
    assert len(env.responder.prompts) == calls_after_first, "第二次不该再花模型调用"
    assert {page["concept_id"]: page["chars"] for page in env.service.wiki_pages(course_id=env.math.id)} \
        == pages_after_first


def test_indexing_the_same_material_twice_leaves_one_set_of_chunks(env):
    """连点两次索引：第二遍替换掉第一遍的分片，不是叠上去。叠了检索会同一段返回两次。"""
    material = env.service.upload_material(
        course_id=env.math.id, filename="notes.md", mime_type="text/markdown", content=WIKI_MATERIAL.encode(),
    )
    env.run_job(env.service.enqueue_index(material_id=material.id).id)
    once = env.service._repository.list_material_chunks(material_id=material.id)

    env.run_job(env.service.enqueue_index(material_id=material.id).id)

    twice = env.service._repository.list_material_chunks(material_id=material.id)
    assert len(twice) == len(once)
    assert [row["content"] for row in twice] == [row["content"] for row in once]
    assert len(env.service.search_course(course_id=env.math.id, query="极限")) == 2


def test_wiki_prune_removes_pages_whose_concept_is_gone(env):
    """重建索引会换掉概念列表（比如从刮标题改成读目录书签）。旧概念的页文件不会
    自己消失，知识页里就混着一堆不存在的概念——看上去像功能坏了。"""
    store = WikiStore(env.settings.data_dir)
    for concept_id in ("keep", "stale_a", "stale_b"):
        store.write(course_id=env.math.id, concept_id=concept_id, concept_name=concept_id,
                    body="正文", source_hash="h", source_refs=[], updated_at="2026-07-28T00:00:00+00:00")
    assert len(store.list_pages(course_id=env.math.id)) == 3

    removed = store.prune(course_id=env.math.id, valid_concept_ids={"keep"})
    assert sorted(removed) == ["stale_a", "stale_b"]
    assert [page.concept_id for page in store.list_pages(course_id=env.math.id)] == ["keep"]
    # 概念全在时不该动任何文件
    assert store.prune(course_id=env.math.id, valid_concept_ids={"keep"}) == []


def test_a_full_queue_rejects_instead_of_leaving_the_job_queued(env, monkeypatch):
    """队列满时必须把 job 标成 failed 并说清原因。留在 queued 的话前端会一直
    转圈等一个永远不会被执行的任务。容量是 max(workers, queue_capacity)，
    所以要真占满一个槽位才测得到这条路径。"""
    import threading

    release = threading.Event()
    original = env.service.run_job
    monkeypatch.setattr(env.service, "run_job", lambda *, job_id: release.wait(10) and original(job_id=job_id))

    worker = KnowledgeJobWorker(env.service, workers=1, queue_capacity=1)
    worker.start()
    try:
        jobs = []
        for name in ("first.md", "second.md"):
            material = env.service.upload_material(
                course_id=env.math.id, filename=name, mime_type="text/markdown", content=b"a",
            )
            jobs.append(env.service.enqueue_index(material_id=material.id))
        assert worker.submit(jobs[0].id) is True, "第一个应该占住唯一的槽位"
        assert worker.submit(jobs[1].id) is False, "槽位被占满，第二个必须被拒"
        rejected = env.service.get_job(job_id=jobs[1].id)
        assert rejected.status == "failed" and "队列已满" in (rejected.error_message or "")
    finally:
        release.set()
        worker.shutdown()


def test_a_shutting_down_worker_rejects_new_jobs(env):
    """关闭中还收任务，等于收下一个不会被执行的承诺。"""
    worker = KnowledgeJobWorker(env.service, workers=1, queue_capacity=4)
    worker.start()
    worker.shutdown()
    material = env.service.upload_material(
        course_id=env.math.id, filename="late.md", mime_type="text/markdown", content=b"a",
    )
    job = env.service.enqueue_index(material_id=material.id)
    assert worker.submit(job.id) is False
    assert env.service.get_job(job_id=job.id).status == "failed"
