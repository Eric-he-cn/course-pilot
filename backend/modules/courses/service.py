from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Callable, Sequence
from core.common import new_id, utc_now
from .models import Course
from .repository import CourseRepository
_COLORS = ("#B56E3D", "#176B5B", "#365F91", "#8C5B96", "#9A650D", "#B23A36")
def _unlink(paths: list[Path]) -> None:
    for path in paths:
        try: path.unlink(missing_ok=True)
        except OSError: pass
class CourseService:
    def __init__(self, repository: CourseRepository, *,
                 purge_sessions: Callable | None = None, purge_materials: Callable | None = None,
                 purge_material: Callable | None = None,
                 purge_course_files: Sequence[Callable[..., None]] = ()) -> None:
        self._repository = repository
        # 会话与教材的连带删除由组装根注入：跨模块的删除顺序只在这里编排一次。
        # 落盘目录同理——各模块自己知道东西放在哪，这里只负责按顺序叫一遍。
        self._purge_sessions, self._purge_materials, self._purge_material = purge_sessions, purge_materials, purge_material
        self._purge_course_files = tuple(purge_course_files)
    def delete_course(self, course_id: str) -> None:
        """课程连同它的会话、教材、计划与证据一起删。"""
        paths = self._repository.delete(course_id, purge_sessions=self._purge_sessions, purge_materials=self._purge_materials)
        if paths is None: raise LookupError("课程不存在")
        # 磁盘清理放在事务提交之后：文件删了回滚不回来，宁可留孤儿文件也不留孤儿记录。
        _unlink(paths)
        for purge in self._purge_course_files: purge(course_id=course_id)
    def delete_material(self, material_id: str) -> None:
        """教材连带它的分片、概念目录与掌握度投影一起删。"""
        path = self._purge_material(material_id)
        if path is None: raise LookupError("教材不存在")
        _unlink([path])
    @staticmethod
    def _course(row) -> Course: return Course(row["id"], row["name"], row["color"], bool(row["wiki_enabled"]), row["created_at"], row["updated_at"])
    def list_courses(self) -> list[Course]: return [self._course(row) for row in self._repository.list_rows()]
    def get_course(self, course_id: str) -> Course | None:
        row = self._repository.get_row(course_id); return self._course(row) if row else None
    def create_course(self, *, name: str, color: str | None = None) -> Course:
        clean_name = name.strip()
        if not clean_name: raise ValueError("课程名称不能为空")
        if len(clean_name) > 100: raise ValueError("课程名称不能超过 100 个字符")
        chosen = color or _COLORS[len(self.list_courses()) % len(_COLORS)]
        if not chosen.startswith("#") or len(chosen) != 7: raise ValueError("课程颜色必须是 #RRGGBB")
        course_id, timestamp = new_id("course"), utc_now()
        # 课程名在库里唯一，重名要给用户明确提示，而不是让约束冲突冒成 500。
        try: self._repository.insert(course_id=course_id, name=clean_name, color=chosen, timestamp=timestamp)
        except sqlite3.IntegrityError as error: raise ValueError("课程名称已存在") from error
        return self.get_course(course_id)  # type: ignore[return-value]
    def update_course(self, course_id: str, *, wiki_enabled: bool | None = None, name: str | None = None) -> Course | None:
        if not self.get_course(course_id): return None
        if name is not None and not name.strip(): raise ValueError("课程名称不能为空")
        try: self._repository.update(course_id, name=name.strip() if name is not None else None, wiki_enabled=wiki_enabled, timestamp=utc_now())
        except sqlite3.IntegrityError as error: raise ValueError("课程名称已存在") from error
        return self.get_course(course_id)
