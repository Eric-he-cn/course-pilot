"""云端嵌入与重排适配器：给跑不动本地模型的机器用的那条路。

这个模块此前零覆盖，而它全部的价值都在降级分支上——半截向量、维度不一致、
服务端返回乱序或报错。这些分支静默返回 None/空列表让调用方退回词面检索，
写错了不会有任何报错，只是检索质量悄悄变差。
"""
from __future__ import annotations

import struct

import pytest

from adapters.cloud_retrieval import CloudEmbedder, CloudReranker


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeClient:
    """按调用次数依次返回预设响应；异常用 Exception 实例表示。"""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None):  # noqa: A002
        self.calls.append({"url": url, "json": json})
        item = self._responses.pop(0) if self._responses else FakeResponse({"data": []})
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        return None


def _embedder(client) -> CloudEmbedder:
    embedder = CloudEmbedder(api_key="k", base_url="https://example.test/v1", model="m")
    embedder._client = client
    return embedder


def _floats(raw: bytes) -> list[float]:
    return [round(value, 4) for value in struct.unpack(f"<{len(raw) // 4}f", raw)]


def test_embeddings_are_reordered_by_index():
    """服务端不保证按输入顺序返回。错序会让每个 chunk 配上别人的向量，
    检索结果全乱但不报错。"""
    client = FakeClient(FakeResponse({"data": [
        {"index": 1, "embedding": [0.0, 1.0]},
        {"index": 0, "embedding": [1.0, 0.0]},
    ]}))
    vectors = _embedder(client).embed_documents(["第一段", "第二段"])
    assert [_floats(item) for item in vectors] == [[1.0, 0.0], [0.0, 1.0]]


def test_a_failed_batch_discards_the_whole_request():
    """半截向量比没有向量更糟：维度对不上会让整个库的检索静默出错。"""
    embedder = _embedder(FakeClient(RuntimeError("网关超时")))
    assert embedder.embed_documents(["一段"]) is None
    assert "RuntimeError" in str(embedder.status()["error"])


def test_http_error_degrades_instead_of_raising():
    embedder = _embedder(FakeClient(FakeResponse({}, status=500)))
    assert embedder.embed_documents(["一段"]) is None
    assert embedder.status()["error"]


def test_rank_returns_cosine_scores_sorted_desc():
    client = FakeClient(FakeResponse({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}))
    vectors = [struct.pack("<2f", 1.0, 0.0), struct.pack("<2f", 0.0, 1.0)]
    ranked = _embedder(client).rank(query="q", vectors=vectors, top_k=2)
    assert [index for index, _ in ranked] == [0, 1]
    assert ranked[0][1] > ranked[1][1]


def test_rank_refuses_to_score_when_dimensions_disagree():
    """换过嵌入模型但没重建索引。给出错的排序比返回空更糟。"""
    client = FakeClient(FakeResponse({"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}))
    embedder = _embedder(client)
    assert embedder.rank(query="q", vectors=[struct.pack("<2f", 1.0, 0.0)], top_k=1) == []
    assert "维度不一致" in str(embedder.status()["error"])


def test_rank_without_vectors_makes_no_request():
    client = FakeClient()
    assert _embedder(client).rank(query="q", vectors=[], top_k=5) == []
    assert client.calls == [], "库里没有向量时不该白花一次云端调用"


def _reranker(client) -> CloudReranker:
    reranker = CloudReranker(api_key="k", url="https://example.test/rerank", model="m")
    reranker._client = client
    return reranker


def test_rerank_scores_come_back_in_document_order():
    """服务端按相关度倒序返回，调用方要的是「第 i 篇文档得几分」。
    不按 index 排回去，阈值过滤就会滤掉错的那几条。"""
    client = FakeClient(FakeResponse({"results": [
        {"index": 2, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.5},
        {"index": 1, "relevance_score": 0.1},
    ]}))
    assert _reranker(client).rerank(query="q", documents=["a", "b", "c"]) == [0.5, 0.1, 0.9]


def test_rerank_returns_none_on_failure_so_caller_falls_back_to_rrf():
    reranker = _reranker(FakeClient(RuntimeError("连接被拒")))
    assert reranker.rerank(query="q", documents=["a"]) is None
    assert reranker.status()["error"]


def test_rerank_with_no_documents_makes_no_request():
    client = FakeClient()
    assert _reranker(client).rerank(query="q", documents=[]) is None
    assert client.calls == []
