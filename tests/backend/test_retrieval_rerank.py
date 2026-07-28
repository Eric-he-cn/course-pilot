"""重排与阈值：查不到就返回空，而不是塞一堆无关片段。

这里一律用假的向量与重排适配器。真模型有 2 GB，check.sh 不发网络请求也不下模型；
要验证的是「阈值真的把低分丢掉了」这条逻辑，不是模型质量。模型质量在
Docs/工作目录/ 里用真实语料标定过。
"""
from __future__ import annotations

import time

import numpy as np
import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import Settings

MATERIAL = (
    "Round Robin 把时间片轮转给每个任务。时间片过长会退化成 FCFS，过短则上下文切换开销占比升高。\n"
    "STCF 优先跑剩余时间最短的任务，平均周转时间最优，代价是长任务可能长期得不到调度。\n"
    "批量规范化通过标准化中间层输出来加速训练，它让每层的输入分布更稳定。\n"
)


class FakeEmbedder:
    """维度很小的确定性向量：只要能让 dense 路召回出东西就够。"""

    name = "fake-embedder"

    def status(self) -> dict[str, object]:
        return {"model": self.name, "loaded": True, "error": None}

    @staticmethod
    def _vector(text: str) -> np.ndarray:
        buckets = np.zeros(8, dtype=np.float32)
        for index, char in enumerate(text):
            buckets[ord(char) % 8] += 1
        norm = float(np.linalg.norm(buckets)) or 1.0
        return buckets / norm

    def embed_documents(self, texts: list[str]) -> list[bytes]:
        return [self._vector(text).tobytes() for text in texts]

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
        matrix = np.frombuffer(b"".join(vectors), dtype=np.float32).reshape(len(vectors), -1)
        scores = matrix @ self._vector(query)
        return [(int(i), float(scores[i])) for i in np.argsort(-scores)[:top_k]]


class ScriptedReranker:
    """按关键词给分：命中就高分，否则低分。None 表示模型不可用。"""

    name = "scripted-reranker"

    def __init__(self, *, keyword: str | None, unavailable: bool = False) -> None:
        self.keyword = keyword
        self.unavailable = unavailable
        self.calls: list[int] = []

    def status(self) -> dict[str, object]:
        return {"model": self.name, "loaded": not self.unavailable, "error": "boom" if self.unavailable else None}

    def rerank(self, *, query: str, documents: list[str]) -> list[float] | None:
        if self.unavailable:
            return None
        self.calls.append(len(documents))
        if self.keyword is None:
            return [0.001] * len(documents)
        return [0.9 if self.keyword in document else 0.001 for document in documents]


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="https://api.example.com/v1", text_api_key="",
        text_model="example-model", enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
        rag_min_rerank_score=0.2,
    )


def _wait_for_job(client, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} 没有结束")


def _indexed_course(client, name: str = "操作系统"):
    course = client.post("/api/v2/courses", json={"name": name}).json()
    upload = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("os.md", MATERIAL, "text/markdown")},
    ).json()
    job = client.post(f"/api/v2/materials/{upload['id']}/index").json()
    assert _wait_for_job(client, job["id"])["status"] == "completed"
    return course


def _search(client, course_id: str, query: str) -> list[dict]:
    response = client.post(f"/api/v2/courses/{course_id}/knowledge/search", json={"query": query})
    assert response.status_code == 200
    return response.json()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _install(client, reranker) -> None:
    space = workspace(client)
    space.knowledge._embedder = FakeEmbedder()
    space.knowledge._reranker = reranker


def test_everything_below_the_floor_means_nothing_was_found(client):
    """这是这次改造的目的：查不到的时候返回空，别把召回的前 6 条硬塞进引用。"""
    _install(client, ScriptedReranker(keyword=None))
    course = _indexed_course(client)
    assert _search(client, course["id"], "宋朝的科举制度是怎么运作的") == []


def test_only_the_chunks_above_the_floor_survive(client):
    _install(client, ScriptedReranker(keyword="Round Robin"))
    course = _indexed_course(client)
    hits = _search(client, course["id"], "时间片怎么选")
    assert hits, "命中的那条应该留下来"
    assert all("Round Robin" in hit["text"] for hit in hits)
    # 分数换成了 rerank 分，界面上那个「检索排序分」才有意义
    assert all(hit["score"] == pytest.approx(0.9) for hit in hits)


def test_unavailable_reranker_falls_back_instead_of_returning_empty(client):
    """模型加载失败不能让检索变成永远查不到——那是把降级做成了故障。"""
    _install(client, ScriptedReranker(keyword=None, unavailable=True))
    course = _indexed_course(client)
    assert _search(client, course["id"], "时间片怎么选"), "重排不可用时应退回 RRF 排序"


def test_candidate_pool_is_wider_than_the_returned_limit(client):
    """召回宽、精排窄：候选数按 rag_rerank_candidates 走，不是按最终条数。"""
    reranker = ScriptedReranker(keyword="STCF")
    _install(client, reranker)
    course = _indexed_course(client)
    _search(client, course["id"], "调度")
    assert reranker.calls, "应该调用过重排"
    assert reranker.calls[0] > 1


def test_health_reports_the_reranker_and_its_floor(client):
    _install(client, ScriptedReranker(keyword="Round Robin"))
    rag = client.get("/api/v2/health").json()["rag"]
    assert rag["reranker"]["min_score"] == 0.2
    assert rag["backend"] == "hybrid_bge_rerank"


# 真实语料标定出的 rerank 分（bge-reranker-v2-m3，候选 dense top-20）。同一批问题打到
# 「操作系统」和「深度学习」两个库，对一个库是正例、对另一个是负例。完整数据与脚本在
# Docs/工作目录/。这里留下边界值，是为了让阈值的改动必须先面对这批数据。
CALIBRATION_POSITIVE = (0.0507, 0.7353, 0.8779, 0.8827, 0.8835, 0.94, 0.9745, 0.9773, 0.978, 0.9899, 0.9903, 0.9921, 0.9958)
CALIBRATION_NEGATIVE = (0.0001, 0.0002, 0.0005, 0.0009, 0.0011, 0.0013, 0.0021, 0.0027, 0.0056, 0.0118, 0.0122, 0.0598, 0.182)


def test_the_default_threshold_still_separates_the_calibration_set():
    """守住阈值这个决定本身：调它之前先看这批实测数据还分不分得开。"""
    from core.settings import CALIBRATED_RERANK_THRESHOLDS

    threshold = CALIBRATED_RERANK_THRESHOLDS["BAAI/bge-reranker-v2-m3"]
    assert max(CALIBRATION_NEGATIVE) < threshold, "负例会漏过阈值"
    # 正例最低那条是问法敏感的孤点，排掉它之后整簇要在阈值之上
    assert sorted(CALIBRATION_POSITIVE)[1] > threshold, "正例主簇会被误杀"
    kept = [score for score in CALIBRATION_POSITIVE if score >= threshold]
    assert len(kept) >= len(CALIBRATION_POSITIVE) - 1


def test_an_uncalibrated_model_does_not_get_someone_elses_threshold(monkeypatch, tmp_path):
    """分数尺度跟着模型走。没标定过就不过滤，别拿别人的阈值误杀教材内容。"""
    monkeypatch.setenv("RAG_RERANKER_MODEL", "some-org/unknown-reranker")
    monkeypatch.setenv("STORAGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("RAG_MIN_RERANK_SCORE", raising=False)
    assert Settings.from_environment(tmp_path).rag_min_rerank_score == 0.0

    monkeypatch.setenv("RAG_MIN_RERANK_SCORE", "0.45")
    assert Settings.from_environment(tmp_path).rag_min_rerank_score == 0.45


def test_health_does_not_claim_rerank_when_the_model_failed(client):
    """加载失败时阈值不生效，backend 不能继续报 rerank，否则会以为过滤还在工作。"""
    _install(client, ScriptedReranker(keyword=None, unavailable=True))
    rag = client.get("/api/v2/health").json()["rag"]
    assert rag["backend"] != "hybrid_bge_rerank"
    assert rag["reranker"]["error"]


# ---- 弱机器分档 ----

def test_hardware_tiers_only_kick_in_for_auto():
    """写死模型名就照配置来。分档只在配置写 auto 时生效，不做静默替换。"""
    from core import hardware

    tight = hardware.Hardware(total_ram_gib=6.0, cpu_count=4, accelerator="cpu", tier="small")
    assert hardware.resolve("embedding", "BAAI/bge-base-zh-v1.5", tight) == "BAAI/bge-base-zh-v1.5"
    assert hardware.resolve("embedding", "auto", tight) == "BAAI/bge-small-zh-v1.5"
    assert hardware.resolve("reranker", "auto", tight) == "BAAI/bge-reranker-base"


def test_the_small_tier_reranker_is_also_calibrated():
    """降档不能顺手丢掉「查不到返回空」——小档那个重排模型必须也标定过。"""
    from core import hardware
    from core.settings import CALIBRATED_RERANK_THRESHOLDS

    assert hardware.SMALL["reranker"] in CALIBRATED_RERANK_THRESHOLDS


def test_the_minimal_tier_turns_the_reranker_off_entirely():
    """内存太小的时候留着重排会把每次检索拖到几秒，关掉比降档更诚实。"""
    from core import hardware

    assert hardware.MINIMAL["reranker"] == ""
    starved = hardware.Hardware(total_ram_gib=2.0, cpu_count=2, accelerator="cpu", tier="minimal")
    assert hardware.resolve("reranker", "auto", starved) == ""


@pytest.mark.parametrize("ram, accelerator, expected", [
    (2.0, "cpu", "minimal"),      # 内存不够，两个模型都不加载
    (6.0, "cpu", "small"),        # 够跑小模型
    (32.0, "cpu", "full"),
    (2.0, "mps", "full"),         # 统一内存/独显不按内存降档
    (2.0, "cuda", "full"),
    (0.0, "cpu", "full"),         # 读不到内存宁可按满档，让加载失败后自然降级
])
def test_tier_is_decided_by_ram_unless_there_is_an_accelerator(monkeypatch, ram, accelerator, expected):
    """分档判定本身要测到每一档。断言 tier 落在三档取值域里是恒真的，测不出任何东西。"""
    from core import hardware

    monkeypatch.setattr(hardware, "_total_ram_gib", lambda: ram)
    monkeypatch.setattr(hardware, "_accelerator", lambda: accelerator)
    assert hardware.probe().tier == expected
