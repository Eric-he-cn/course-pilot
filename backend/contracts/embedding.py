from __future__ import annotations

from typing import Protocol


class EmbedderPort(Protocol):
    """向量化端口：向量以 float32 字节串存取，调用方不接触模型与 numpy。

    embed_documents 在依赖或模型不可用时返回 None，调用方据此降级为纯词面检索。
    """

    @property
    def name(self) -> str: ...

    def status(self) -> dict[str, object]: ...

    def embed_documents(self, texts: list[str]) -> list[bytes] | None: ...

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]: ...

    # 文档两两之间的余弦相似度矩阵。rank 顶不了这件事：它按查询侧口径编码（BGE 中文模型
    # 要加非对称前缀），两端都是文档时那个前缀就是噪声。算不出来（缺 numpy、库里维度不齐）返回 None。
    def pairwise(self, vectors: list[bytes]) -> list[list[float]] | None: ...
