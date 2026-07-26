from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class PlanItem:
    id: str; due_date: str; title: str; concept_id: str | None; status: str; concept_name: str | None = None
@dataclass(frozen=True)
class Plan:
    id: str; course_id: str; status: str; version: int; created_at: str; updated_at: str; items: list[PlanItem]
@dataclass(frozen=True)
class PlanDiff:
    """一次写入的结果：版本变化与条目增减，供工具回执与审计使用。"""
    version_from: int; version_to: int; added: int; removed: int; kept_past: int; kept_locked: int
