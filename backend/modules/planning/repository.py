from __future__ import annotations
from core.store import SQLiteStore
class PlanningRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def get_active_plan_row(self, *, course_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM plans WHERE course_id = ? AND status = 'active'", (course_id,)).fetchone()
    def list_item_rows(self, *, plan_id: str):
        with self._store.read() as c:
            return c.execute(
                "SELECT i.*, c.name AS concept_name FROM plan_items i LEFT JOIN concepts c ON c.id = i.concept_id"
                " WHERE i.plan_id = ? ORDER BY i.due_date ASC, i.created_at ASC", (plan_id,)
            ).fetchall()
    def write(self):
        """写事务：版本校验、条目重写与 revision 必须在同一个连接里完成。"""
        return self._store.write()
