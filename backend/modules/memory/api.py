"""长期记忆的对外接口。别处只经这里，不碰 MemoryStore 的落盘细节。"""
from __future__ import annotations

from typing import Protocol


class MemoryStorePort(Protocol):
    def read_user(self) -> str: ...
    def read_course(self, course_id: str) -> str: ...
    def patch(self, *, scope: str, section: str, content: str, course_id: str | None = None) -> str: ...
