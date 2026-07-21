from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class PlanItem:
    id: str; due_date: str; title: str; concept_id: str | None; status: str
@dataclass(frozen=True)
class Plan:
    id: str; course_id: str; status: str; version: int; created_at: str; updated_at: str; items: list[PlanItem]
