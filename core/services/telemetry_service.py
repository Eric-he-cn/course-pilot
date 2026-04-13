"""Thin telemetry facade used by runtime/services."""

from __future__ import annotations

from typing import Any

from core.metrics import add_event, get_active_trace


class TelemetryService:
    """Central place for trace events and fallback reporting."""

    @staticmethod
    def trace_tag() -> str:
        trace = get_active_trace()
        if trace is None:
            return ""
        request_id = str((trace.meta or {}).get("request_id", "")).strip() or "unknown"
        return f" request_id={request_id} trace_id={trace.trace_id}"

    @staticmethod
    def add_event(event_type: str, **payload: Any) -> None:
        add_event(event_type, **payload)

    def record_fallback(
        self,
        *,
        session_id: str,
        request_id: str,
        agent: str,
        step_name: str,
        reason: str,
        from_path: str,
        to_path: str,
    ) -> None:
        self.add_event(
            "runtime_fallback",
            session_id=session_id,
            request_id=request_id,
            agent=agent,
            step_name=step_name,
            reason=reason,
            from_path=from_path,
            to_path=to_path,
        )
