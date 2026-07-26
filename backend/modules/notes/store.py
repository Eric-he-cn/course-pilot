"""课程笔记：学习卡片、整理稿等由 Agent 落盘的 markdown，按课程隔离。"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAX_TITLE_CHARS = 60
MAX_NOTE_BYTES = 256 * 1024
MAX_NOTES_PER_COURSE = 200
# 文件名白名单：中文、字母数字、空格与少量连接符，其余一律剔除。
_ALLOWED = re.compile(r"[^\w一-鿿 \-_.]", re.UNICODE)


@dataclass(frozen=True)
class Note:
    title: str
    chars: int
    updated_at: str


class NoteStore:
    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir / "notes"

    def _course_dir(self, course_id: str) -> Path:
        return self._root / _safe_component(course_id)

    def _path(self, *, course_id: str, title: str) -> Path:
        """落点必须仍在本课程笔记目录内，且不能是符号链接。"""
        directory = self._course_dir(course_id)
        name = _safe_component(title)
        if not name:
            raise ValueError("笔记标题不能为空，且只能含中文、字母、数字与 - _ 空格")
        path = (directory / f"{name}.md").resolve()
        base = directory.resolve()
        if os.path.commonpath([str(base), str(path)]) != str(base):
            raise ValueError("笔记只能写在本课程的笔记目录里")
        if path.is_symlink():
            # abspath/commonpath 都不解析符号链接，链接可以把写入带出目录。
            raise ValueError("笔记文件是符号链接，已拒绝写入")
        return path

    def write(self, *, course_id: str, title: str, content: str, mode: str = "write") -> Note:
        if mode not in {"write", "append"}:
            raise ValueError("mode 只能是 write 或 append")
        if not content.strip():
            raise ValueError("笔记内容不能为空")
        if len(content.encode("utf-8")) > MAX_NOTE_BYTES:
            raise ValueError(f"单篇笔记不能超过 {MAX_NOTE_BYTES // 1024} KiB")
        path = self._path(course_id=course_id, title=title)
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if not path.exists() and sum(1 for _ in directory.glob("*.md")) >= MAX_NOTES_PER_COURSE:
            raise ValueError(f"本课程笔记已达上限 {MAX_NOTES_PER_COURSE} 篇")
        body = content if content.endswith("\n") else content + "\n"
        with path.open("a" if mode == "append" else "w", encoding="utf-8") as handle:
            handle.write(body)
        return self._describe(path)

    def read(self, *, course_id: str, title: str) -> str:
        path = self._path(course_id=course_id, title=title)
        if not path.is_file():
            raise LookupError(f"没有名为「{title}」的笔记")
        return path.read_text(encoding="utf-8")

    def list_notes(self, *, course_id: str) -> list[Note]:
        directory = self._course_dir(course_id)
        if not directory.is_dir():
            return []
        return sorted(
            (self._describe(path) for path in directory.glob("*.md") if path.is_file() and not path.is_symlink()),
            key=lambda note: note.updated_at, reverse=True,
        )

    @staticmethod
    def _describe(path: Path) -> Note:
        stat = path.stat()
        from datetime import datetime, timezone
        return Note(
            title=path.stem, chars=len(path.read_text(encoding="utf-8")),
            updated_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        )


def _safe_component(raw: str) -> str:
    """NFC 归一化后取 basename 并过白名单：APFS 对归一化不敏感，
    写入与读取用不同形式会读不到同一个文件。"""
    text = unicodedata.normalize("NFC", str(raw or "")).strip()
    name = os.path.basename(text).strip().strip(".")
    return _ALLOWED.sub("", name)[:MAX_TITLE_CHARS].strip()
