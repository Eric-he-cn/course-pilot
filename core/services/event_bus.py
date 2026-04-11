"""Structured event helpers for stream/runtime hidden events."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class EventBus:
    """Build hidden SSE payloads without coupling API code to business logic."""

    @staticmethod
    def status(message: str) -> Dict[str, Any]:
        return {"__status__": str(message or "")}

    @staticmethod
    def citations(items: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        return {"__citations__": list(items or [])}

    @staticmethod
    def context_budget(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"__context_budget__": dict(payload or {})}

    @staticmethod
    def tool_calls(payload: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        return {"__tool_calls__": list(payload or [])}


_DEFAULT_EVENT_BUS = EventBus()


def get_default_event_bus() -> EventBus:
    return _DEFAULT_EVENT_BUS
