from __future__ import annotations
from typing import Protocol
from .models import Plan, PlanDiff


class PlanConflictError(ValueError):
    """期望版本与当前版本不一致：调用方要重读计划再重算，不能盲目重试。"""

    def __init__(self, current_version: int) -> None:
        super().__init__(f"计划已变化，当前版本是 v{current_version}")
        self.current_version = current_version


class PlanReaderPort(Protocol):
    def get_plan(self, *, course_id: str) -> Plan | None: ...


class PlanWriterPort(Protocol):
    """写入侧单独成 port：只有 plan_update 工具用到它。"""
    def update_plan(self, *, course_id: str, expected_version: int, items: list[dict], note: str | None = None, turn_id: str | None = None) -> PlanDiff: ...
