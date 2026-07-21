from __future__ import annotations
from core.store import SQLiteStore
class LearningRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def count_events(self, *, course_id: str) -> int:
        with self._store.read() as c: return int(c.execute("SELECT count(*) FROM evidence_events WHERE course_id = ?", (course_id,)).fetchone()[0])
    def list_recent_event_rows(self, *, course_id: str, limit: int):
        with self._store.read() as c: return c.execute("SELECT * FROM evidence_events WHERE course_id = ? ORDER BY created_at DESC LIMIT ?", (course_id, limit)).fetchall()
