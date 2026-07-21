from __future__ import annotations
from .models import Plan, PlanItem
from .repository import PlanningRepository
class PlanningService:
    """计划的只读骨架：表结构与查询已就位，plan_update 写链路随规划功能落地。"""
    def __init__(self, repository: PlanningRepository) -> None: self._repository = repository
    def get_plan(self, *, course_id: str) -> Plan | None:
        row = self._repository.get_active_plan_row(course_id=course_id)
        if row is None: return None
        items = [PlanItem(item["id"], item["due_date"], item["title"], item["concept_id"], item["status"]) for item in self._repository.list_item_rows(plan_id=row["id"])]
        return Plan(row["id"], row["course_id"], row["status"], row["version"], row["created_at"], row["updated_at"], items)
