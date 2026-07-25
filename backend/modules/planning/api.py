from __future__ import annotations
from typing import Protocol
from .models import Plan


class PlanReaderPort(Protocol):
    def get_plan(self, *, course_id: str) -> Plan | None: ...
