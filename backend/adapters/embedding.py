from __future__ import annotations

import threading

# BGE 中文检索模型要求查询侧加指令前缀，文档侧不加；bge-m3 内置多语言指令无需前缀。
_BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
# 单次持锁编码的条数上限：越小，索引期间的查询等待越短。
_ENCODE_LOCK_STEP = 64


class BgeEmbedder:
    """Sentence-Transformers 向量适配器：懒加载，加载失败后保持不可用不再重试。"""

    def __init__(self, *, model_name: str, device: str = "auto", batch_size: int = 256) -> None:
        self._model_name = model_name
        self._device = device
        self._batch_size = max(1, batch_size)
        self._lock = threading.Lock()
        self._model = None
        self._error: str | None = None

    @property
    def name(self) -> str:
        return self._model_name

    def status(self) -> dict[str, object]:
        return {"model": self._model_name, "loaded": self._model is not None, "error": self._error}

    def _load(self):
        with self._lock:
            if self._model is None and self._error is None:
                try:
                    from sentence_transformers import SentenceTransformer

                    device = None if self._device in {"", "auto"} else self._device
                    self._model = SentenceTransformer(self._model_name, device=device)
                except Exception as error:
                    self._error = f"{type(error).__name__}: {error}"
            return self._model

    def embed_documents(self, texts: list[str]) -> list[bytes] | None:
        model = self._load()
        if model is None:
            return None
        import numpy as np

        # 索引 worker 与查询线程共享一个模型实例，encode 必须互斥；分段持锁，
        # 否则索引一本大教材期间提问要排队等整批编码完成。
        step = min(self._batch_size, _ENCODE_LOCK_STEP)
        vectors: list[bytes] = []
        for start in range(0, len(texts), step):
            with self._lock:
                encoded = model.encode(texts[start:start + step], batch_size=step, normalize_embeddings=True, show_progress_bar=False)
            vectors.extend(np.asarray(vector, dtype=np.float32).tobytes() for vector in encoded)
        return vectors

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
        model = self._load()
        if model is None or not vectors:
            return []
        import numpy as np

        prefixed = _BGE_ZH_QUERY_PREFIX + query if self._needs_query_prefix() else query
        with self._lock:
            query_vector = model.encode([prefixed], normalize_embeddings=True, show_progress_bar=False)[0]
        # ponytail: 每次全量反序列化 + 暴力点积，万级 chunk 内足够；更大规模换内存缓存或 FAISS。
        matrix = np.frombuffer(b"".join(vectors), dtype=np.float32).reshape(len(vectors), -1)
        scores = matrix @ np.asarray(query_vector, dtype=np.float32)
        order = np.argsort(-scores)[: max(1, top_k)]
        return [(int(index), float(scores[index])) for index in order]

    def _needs_query_prefix(self) -> bool:
        name = self._model_name.lower()
        return "bge" in name and "m3" not in name and ("zh" in name or "chinese" in name)
