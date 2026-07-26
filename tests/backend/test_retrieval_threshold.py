"""相似度阈值：最像的一块都不达标时，这次检索按「没搜到」处理。

真实向量模型那条用例默认跳过（要下模型、跑得慢），需要时显式开：
    COURSEPILOT_TEST_EMBEDDINGS=1 PYTHONPATH=backend python -m pytest tests/backend/test_retrieval_threshold.py -q -s
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import replace

import pytest

from core.settings import Settings
from core.store import SQLiteStore
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker

CALCULUS = """3.3 链式法则
如果 u=g(x) 在点 x 可导，而 y=f(u) 在对应点 u 可导，则复合函数 y=f(g(x)) 在点 x 可导，
且其导数为 dy/dx = f'(u) * g'(x)。也就是说，先对外层函数求导，再乘以内层函数的导数。
例如 y=sin(2x) 的导数为 2cos(2x)。链式法则是复合函数求导的核心工具。

3.5 高阶导数
函数 f(x) 的导数 f'(x) 仍然是 x 的函数，如果 f'(x) 仍可导，其导数称为二阶导数，记作 f''(x)。
二阶导数刻画曲线的凹凸性：f''(x)>0 时曲线是凹的，f''(x)<0 时曲线是凸的。

4.1 罗尔定理
若 f(x) 在闭区间 [a,b] 上连续，在开区间 (a,b) 内可导，且 f(a)=f(b)，
则在 (a,b) 内至少存在一点 ξ，使 f'(ξ)=0。
"""


class _StubEmbedder:
    """按预设分数打分的假向量模型，用来验证阈值分支而不加载真模型。"""

    def __init__(self, score: float) -> None:
        self.score = score

    @property
    def name(self) -> str:
        return "stub"

    def status(self) -> dict[str, object]:
        return {"model": "stub", "loaded": True, "error": None}

    def embed_documents(self, texts: list[str]) -> list[bytes] | None:
        return [struct.pack("<f", 1.0) for _ in texts]

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
        return [(index, self.score) for index in range(min(len(vectors), top_k))]


def _build(tmp_path, *, embedder, min_similarity: float = 0.2, model: str = "stub"):
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=600, chunk_overlap=120, top_k_results=6,
        rag_embedding_model=model, rag_min_similarity=min_similarity,
    )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    course = CourseService(CourseRepository(store)).create_course(name="高等数学")
    service = KnowledgeService(repository=KnowledgeRepository(store), settings=settings, embedder=embedder)
    worker = KnowledgeJobWorker(service, workers=1, queue_capacity=4)
    worker.start()
    try:
        material = service.upload_material(
            course_id=course.id, filename="calculus.md", mime_type="text/markdown", content=CALCULUS.encode(),
        )
        job = service.enqueue_index(material_id=material.id)
        assert worker.submit(job.id)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            job = service.get_job(job_id=job.id)
            if job and job.status in {"completed", "failed"}:
                break
            time.sleep(0.05)
        assert job.status == "completed", job.error_message
    finally:
        worker.shutdown()
    return service, course, settings


def test_below_threshold_reports_nothing_found(tmp_path):
    service, course, _ = _build(tmp_path, embedder=_StubEmbedder(0.05))
    # 「链式法则」在词面上必定命中，但相似度不达标时整次检索一起丢掉。
    assert service.search_course(course_id=course.id, query="链式法则") == []


def test_above_threshold_returns_hits(tmp_path):
    service, course, _ = _build(tmp_path, embedder=_StubEmbedder(0.9))
    hits = service.search_course(course_id=course.id, query="链式法则")
    assert hits and "链式法则" in hits[0].content


def test_threshold_is_configurable(tmp_path):
    service, course, settings = _build(tmp_path, embedder=_StubEmbedder(0.3), min_similarity=0.5)
    assert service.search_course(course_id=course.id, query="链式法则") == []
    relaxed = KnowledgeService(
        repository=service._repository, settings=replace(settings, rag_min_similarity=0.25), embedder=_StubEmbedder(0.3),
    )
    assert relaxed.search_course(course_id=course.id, query="链式法则")


def test_lexical_only_ignores_threshold(tmp_path):
    """语义检索不可用时没有相似度口径，词面结果照原样返回，不做阈值判定。"""
    service, course, _ = _build(tmp_path, embedder=None, min_similarity=0.99, model="")
    assert service.search_course(course_id=course.id, query="链式法则")


@pytest.mark.skipif(
    not os.environ.get("COURSEPILOT_TEST_EMBEDDINGS"),
    reason="需要真实向量模型，设 COURSEPILOT_TEST_EMBEDDINGS=1 才跑",
)
def test_real_model_separates_relevant_from_chitchat(tmp_path, capsys):
    """实测默认阈值：教材相关的查询都在阈值之上，闲聊都在之下。"""
    from adapters.embedding import BgeEmbedder

    embedder = BgeEmbedder(model_name="BAAI/bge-base-zh-v1.5", device="cpu", batch_size=16)
    service, course, settings = _build(tmp_path, embedder=embedder, model="BAAI/bge-base-zh-v1.5")
    threshold = settings.rag_min_similarity

    def top_score(query: str) -> float:
        hits = service._dense_search(course_id=course.id, query=query, limit=6)
        return max((hit.citation.score for hit in hits), default=0.0)

    relevant = ["链式法则", "罗尔定理", "二阶导数", "复合函数怎么求导", "曲线什么时候是凹的", "sin(2x) 的导数怎么算"]
    chitchat = ["你好", "今天星期几", "谢谢你", "红烧肉怎么做", "推荐一部电影", "你是谁"]
    scores = {query: top_score(query) for query in relevant + chitchat}
    with capsys.disabled():
        print("\n相似度实测（阈值 %.2f）：" % threshold)
        for query, score in scores.items():
            print(f"  {score:.3f}  {'相关' if query in relevant else '闲聊'}  {query}")

    for query in relevant:
        assert scores[query] >= threshold, f"{query} 被误杀：{scores[query]:.3f}"
        assert service.search_course(course_id=course.id, query=query), query
    for query in chitchat:
        assert scores[query] < threshold, f"{query} 未被挡住：{scores[query]:.3f}"
        assert service.search_course(course_id=course.id, query=query) == [], query


def test_threshold_is_off_unless_calibrated():
    """默认必须是关闭的。余弦分的绝对值不跨库可比——实测同一模型下英文教材的分界在
    0.31、中文在 0.37，而「Round Robin」这种教材术语本身只比「在吗」高 0.003。
    给一个通用默认值就等于随机误杀，只能由使用者按自己的库标定。"""
    from core.settings import Settings

    assert Settings.from_environment.__self__ is Settings  # classmethod，下面直接看字段默认值
    field = Settings.__dataclass_fields__["rag_min_similarity"]
    assert field.default == 0.0, "默认值不是 0 就意味着开箱即用地过滤，而它不可靠"
