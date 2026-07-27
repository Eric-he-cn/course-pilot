"""云端嵌入与重排：不装 torch 也能用语义检索。

给跑不动本地模型的机器留的一条路——torch + sentence-transformers 光地板就 379 MiB，
模型文件还要几百 MB 到 2 GB。云端版本零本地依赖，代价是每次检索多一个网络往返，
而且教材内容会发到模型服务商那里（本地模式不会）。

嵌入走 OpenAI 兼容的 /embeddings，这是标准接口，谁家都能接。
重排没有统一标准，这里实现的是 {query, documents} → {results:[{index, relevance_score}]}
这个形状，DashScope、Jina、Cohere、TEI 都接近它，具体 URL 由配置给。
"""
from __future__ import annotations

import threading

import httpx

# 一次请求塞多少条。太大容易撞上服务端的单请求上限，索引本来就是后台任务，不急。
_EMBED_BATCH = 16
# 单条文档送去重排前的截断长度：云端接口对单条有长度上限，600 字符的 chunk 留足余量。
_RERANK_DOC_CHARS = 1000


class CloudEmbedder:
    """OpenAI 兼容的 /embeddings。向量维度由服务端决定，换模型必须重建索引。"""

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_seconds: float = 60) -> None:
        self._model = model
        self._endpoint = f"{base_url.rstrip('/')}/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=timeout_seconds)
        self._lock = threading.Lock()
        self._error: str | None = None

    @property
    def name(self) -> str:
        return f"cloud:{self._model}"

    def status(self) -> dict[str, object]:
        return {"model": self.name, "loaded": self._error is None, "error": self._error}

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            response = self._client.post(
                self._endpoint, headers=self._headers, json={"model": self._model, "input": texts},
            )
            response.raise_for_status()
            rows = response.json()["data"]
            # 服务端不保证按输入顺序返回，按 index 排回去
            return [item["embedding"] for item in sorted(rows, key=lambda item: item.get("index", 0))]
        except Exception as error:
            with self._lock:
                self._error = f"{type(error).__name__}: {error}"
            return None

    def embed_documents(self, texts: list[str]) -> list[bytes] | None:
        import numpy as np

        vectors: list[bytes] = []
        for start in range(0, len(texts), _EMBED_BATCH):
            batch = self._embed(texts[start:start + _EMBED_BATCH])
            if batch is None:
                # 半截的向量比没有向量更糟：维度对不上会让整个库的检索静默出错。
                return None
            vectors.extend(np.asarray(row, dtype=np.float32).tobytes() for row in batch)
        return vectors

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
        import numpy as np

        if not vectors:
            return []
        embedded = self._embed([query])
        if not embedded:
            return []
        query_vector = np.asarray(embedded[0], dtype=np.float32)
        matrix = np.frombuffer(b"".join(vectors), dtype=np.float32).reshape(len(vectors), -1)
        if matrix.shape[1] != query_vector.shape[0]:
            # 换过嵌入模型但没重建索引。返回空让调用方退回词面检索，别给出错的排序。
            with self._lock:
                self._error = f"向量维度不一致：库里 {matrix.shape[1]}，模型给 {query_vector.shape[0]}，需要重建索引"
            return []
        # 服务端可能不做归一化，这里自己归一，保证分数是余弦
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        scores = (matrix / norms) @ (query_vector / (np.linalg.norm(query_vector) or 1.0))
        order = np.argsort(-scores)[: max(1, top_k)]
        return [(int(index), float(scores[index])) for index in order]

    def close(self) -> None:
        self._client.close()


class CloudReranker:
    """{query, documents} → {results:[{index, relevance_score}]}。

    重排没有统一标准，所以 URL 与模型名都从配置来。分数尺度跟着模型走，阈值必须单独标定——
    实测 gte-rerank-v2 给正确文档 0.17，而 bge-reranker-v2-m3 给 0.99，套错阈值会把全部
    结果滤掉。已标定的模型见 core/settings.py 的 CALIBRATED_RERANK_THRESHOLDS。
    """

    def __init__(self, *, api_key: str, url: str, model: str, timeout_seconds: float = 60) -> None:
        self._model = model
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=timeout_seconds)
        self._lock = threading.Lock()
        self._error: str | None = None

    @property
    def name(self) -> str:
        return f"cloud:{self._model}"

    def status(self) -> dict[str, object]:
        return {"model": self.name, "loaded": self._error is None, "error": self._error}

    def rerank(self, *, query: str, documents: list[str]) -> list[float] | None:
        if not documents:
            return None
        clipped = [text[:_RERANK_DOC_CHARS] for text in documents]
        body = {
            "model": self._model,
            "input": {"query": query, "documents": clipped},
            "parameters": {"return_documents": False, "top_n": len(clipped)},
        }
        try:
            response = self._client.post(self._url, headers=self._headers, json=body)
            response.raise_for_status()
            payload = response.json()
            results = payload.get("output", payload).get("results", [])
            scores = [0.0] * len(clipped)
            for item in results:
                index = int(item["index"])
                if 0 <= index < len(scores):
                    scores[index] = float(item.get("relevance_score", item.get("score", 0.0)))
            with self._lock:
                self._error = None
            return scores
        except Exception as error:
            # 一次失败不该打挂整次检索：返回 None 让调用方退回 RRF 排序。
            with self._lock:
                self._error = f"{type(error).__name__}: {error}"
            return None

    def close(self) -> None:
        self._client.close()
