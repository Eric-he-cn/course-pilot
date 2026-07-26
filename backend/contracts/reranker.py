from __future__ import annotations

from typing import Protocol


class RerankerPort(Protocol):
    """重排端口：给 (query, 文档) 打相关性分。

    与向量的余弦距离不同，cross-encoder 把 query 和文档拼在一起过一遍模型，输出的是
    「这段文本回答这个问题吗」。这个分在不同教材库之间可比，所以能拿来做阈值判定。

    模型不可用时 rerank 返回 None，调用方据此退回 RRF 排序并关掉阈值。
    """

    @property
    def name(self) -> str: ...

    def status(self) -> dict[str, object]: ...

    def rerank(self, *, query: str, documents: list[str]) -> list[float] | None: ...
