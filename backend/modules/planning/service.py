from __future__ import annotations
import json
from collections.abc import Callable
from datetime import date
from core.common import new_id, utc_now
from .api import PlanConflictError
from .models import Plan, PlanDiff, PlanItem
from .repository import PlanningRepository

MAX_ITEMS = 120
MAX_TITLE_CHARS = 120


class PlanningService:
    """计划的读写：条目可重写，版本与改动记录不可覆盖。"""

    def __init__(self, repository: PlanningRepository, *, concept_exists: Callable[[str, str], bool] | None = None) -> None:
        self._repository = repository
        # 概念校验由 knowledge 侧注入，planning 不直接查 concepts 表。
        self._concept_exists = concept_exists or (lambda course_id, concept_id: True)

    def get_plan(self, *, course_id: str) -> Plan | None:
        row = self._repository.get_active_plan_row(course_id=course_id)
        if row is None: return None
        items = [
            PlanItem(item["id"], item["due_date"], item["title"], item["concept_id"], item["status"], item["concept_name"])
            for item in self._repository.list_item_rows(plan_id=row["id"])
        ]
        return Plan(row["id"], row["course_id"], row["status"], row["version"], row["created_at"], row["updated_at"], items)

    def update_plan(self, *, course_id: str, expected_version: int, items: list[dict], note: str | None = None, turn_id: str | None = None) -> PlanDiff:
        """重写今天及以后的待办条目；过去的条目与已开始的条目一律保留。

        整批校验，任一条不合法就整批拒绝——半个计划比没有计划更难排查。
        """
        # 本地日期口径：用户说"明天"指的是他所在时区的明天。
        today = date.today().isoformat()
        cleaned = self._validate(course_id=course_id, items=items, today=today)
        timestamp = utc_now()
        with self._repository.write() as connection:
            row = connection.execute("SELECT * FROM plans WHERE course_id = ? AND status = 'active'", (course_id,)).fetchone()
            current_version = row["version"] if row is not None else 0
            if expected_version != current_version:
                raise PlanConflictError(current_version)
            if row is None:
                plan_id = new_id("plan")
                connection.execute(
                    "INSERT INTO plans(id, course_id, status, version, created_at, updated_at) VALUES (?,?,'active',?,?,?)",
                    (plan_id, course_id, current_version, timestamp, timestamp),
                )
            else:
                plan_id = row["id"]
            existing = connection.execute("SELECT due_date, status FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchall()
            kept_past = sum(1 for item in existing if item["due_date"] < today)
            kept_locked = sum(1 for item in existing if item["due_date"] >= today and item["status"] != "pending")
            # 已完成或进行中的未来条目不重写：重建会让状态回退、id 变化。
            removed = connection.execute(
                "DELETE FROM plan_items WHERE plan_id = ? AND due_date >= ? AND status = 'pending'", (plan_id, today)
            ).rowcount
            for entry in cleaned:
                connection.execute(
                    "INSERT INTO plan_items(id, plan_id, due_date, title, concept_id, status, created_at) VALUES (?,?,?,?,?,'pending',?)",
                    (new_id("planitem"), plan_id, entry["due_date"], entry["title"], entry["concept_id"], timestamp),
                )
            version_to = current_version + 1
            diff = PlanDiff(current_version, version_to, len(cleaned), removed, kept_past, kept_locked)
            connection.execute("UPDATE plans SET version = ?, updated_at = ? WHERE id = ?", (version_to, timestamp, plan_id))
            connection.execute(
                "INSERT INTO plan_revisions(id, plan_id, version, turn_id, note, diff_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (new_id("planrev"), plan_id, version_to, turn_id, note,
                 json.dumps({"diff": diff.__dict__, "items": cleaned}, ensure_ascii=False), timestamp),
            )
        return diff

    def _validate(self, *, course_id: str, items: list[dict], today: str) -> list[dict]:
        if not isinstance(items, list) or not items:
            raise ValueError("items 至少要有一个条目")
        if len(items) > MAX_ITEMS:
            raise ValueError(f"条目最多 {MAX_ITEMS} 条，收到 {len(items)} 条")
        cleaned: list[dict] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"第 {index} 条不是对象")
            due_date = str(raw.get("due_date") or "").strip()
            try:
                date.fromisoformat(due_date)
            except ValueError:
                raise ValueError(f"第 {index} 条的 due_date「{due_date}」不是 YYYY-MM-DD") from None
            if due_date < today:
                raise ValueError(f"第 {index} 条的 due_date {due_date} 早于今天（{today}），历史条目不可修改")
            title = " ".join(str(raw.get("title") or "").split())
            if not title:
                raise ValueError(f"第 {index} 条缺少 title")
            if len(title) > MAX_TITLE_CHARS:
                raise ValueError(f"第 {index} 条的 title 超过 {MAX_TITLE_CHARS} 字")
            concept_id = str(raw.get("concept_id") or "").strip() or None
            if concept_id and not self._concept_exists(course_id, concept_id):
                raise ValueError(f"第 {index} 条的 concept_id {concept_id} 不在本课程概念目录里")
            cleaned.append({"due_date": due_date, "title": title, "concept_id": concept_id})
        return cleaned
