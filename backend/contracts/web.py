from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class WebPage:
    url: str
    title: str
    text: str
    truncated: bool = False
    redirect_to: str | None = None  # 跨主机重定向不自动跟随，交回调用方重新校验


@dataclass(frozen=True)
class WebSearchOutcome:
    query: str
    results: list[WebResult] = field(default_factory=list)


class WebSearchPort(Protocol):
    def search(self, *, query: str, limit: int = 5) -> WebSearchOutcome: ...
    def fetch(self, *, url: str) -> WebPage: ...
    def health(self) -> dict[str, object]: ...


class WebAccessError(RuntimeError):
    """出网失败或被安全策略拒绝；code 用于回执与 trace。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
