from __future__ import annotations
from pathlib import Path
from typing import Callable
from core.store import SQLiteStore
# 课程被引用的表，按外键方向排序：引用方先走，courses 最后走。
_COURSE_CASCADE = (
    # 概念可以不挂在任何教材上，教材清完再按课程兜一遍。
    "DELETE FROM concept_mastery WHERE course_id = ?",
    "DELETE FROM concept_aliases WHERE concept_id IN (SELECT id FROM concepts WHERE course_id = ?)",
    "DELETE FROM concepts WHERE course_id = ?",
    # 通用会话留下的解析记录：课程没了，这条痕迹也没有意义。
    "DELETE FROM turn_course_context WHERE resolved_course_id = ?",
    "DELETE FROM plan_revisions WHERE plan_id IN (SELECT id FROM plans WHERE course_id = ?)",
    "DELETE FROM plan_items WHERE plan_id IN (SELECT id FROM plans WHERE course_id = ?)",
    "DELETE FROM plans WHERE course_id = ?",
    "DELETE FROM evidence_events WHERE course_id = ?",
    "DELETE FROM artifacts WHERE course_id = ?",
    # chunks_fts 是虚拟表、没有外键兜底，残留不会被 foreign_key_check 发现。
    "DELETE FROM chunks_fts WHERE course_id = ?",
    "DELETE FROM courses WHERE id = ?",
)
class CourseRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def delete(self, course_id: str, *, purge_sessions: Callable, purge_materials: Callable) -> list[Path] | None:
        """整门课程在一个事务里删完，返回待清理的教材文件路径；课程不存在返回 None。"""
        with self._store.write() as c:
            if c.execute("SELECT 1 FROM courses WHERE id = ?", (course_id,)).fetchone() is None: return None
            purge_sessions(c, course_id=course_id)
            paths = purge_materials(c, course_id=course_id)
            for statement in _COURSE_CASCADE: c.execute(statement, (course_id,))
        return paths
    def list_rows(self):
        with self._store.read() as c: return c.execute("SELECT * FROM courses ORDER BY created_at ASC").fetchall()
    def get_row(self, course_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM courses WHERE id = ?", (course_id,)).fetchone()
    def insert(self, *, course_id: str, name: str, color: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("INSERT INTO courses(id, name, color, wiki_enabled, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)", (course_id, name, color, timestamp, timestamp))
    def update(self, course_id: str, *, name: str | None, wiki_enabled: bool | None, timestamp: str) -> None:
        fields, params = [], []
        if name is not None: fields.append("name = ?"); params.append(name)
        if wiki_enabled is not None: fields.append("wiki_enabled = ?"); params.append(int(wiki_enabled))
        fields.append("updated_at = ?"); params.extend([timestamp, course_id])
        with self._store.write() as c: c.execute(f"UPDATE courses SET {', '.join(fields)} WHERE id = ?", params)
