from __future__ import annotations
from app.store import SQLiteStore
class CourseRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
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
