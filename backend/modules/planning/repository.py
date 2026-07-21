from __future__ import annotations
from core.store import SQLiteStore
class PlanningRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def get_active_plan_row(self, *, course_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM plans WHERE course_id = ? AND status = 'active'", (course_id,)).fetchone()
    def list_item_rows(self, *, plan_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM plan_items WHERE plan_id = ? ORDER BY due_date ASC, created_at ASC", (plan_id,)).fetchall()
