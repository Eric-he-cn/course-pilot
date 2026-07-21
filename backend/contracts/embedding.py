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
