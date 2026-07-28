"""课程笔记的对外接口。"""
from __future__ import annotations

from typing import Protocol

from .store import Note


class NoteStorePort(Protocol):
    def write(self, *, course_id: str, title: str, content: str, mode: str = "write") -> Note: ...
    def read(self, *, course_id: str, title: str) -> str: ...
    def list_notes(self, *, course_id: str) -> list[Note]: ...
