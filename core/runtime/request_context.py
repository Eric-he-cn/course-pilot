"""Request-scoped runtime context helpers."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional


_FIXED_FIELDS = {
    "request_id",
    "course_name",
    "mode",
    "user_id",
    "trace_id",
    "budget_state",
    "tool_audit",
    "idempotency_namespace",
}


@dataclass
class RequestContext:
    """Mutable request-local state shared across runtime and tool layers."""

    request_id: str = ""
    course_name: str = ""
    mode: str = ""
    user_id: str = "default"
    trace_id: str = ""
    budget_state: Dict[str, Any] = field(default_factory=dict)
    tool_audit: List[Dict[str, Any]] = field(default_factory=list)
    idempotency_namespace: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "RequestContext":
        data = dict(payload or {})
        tool_audit = data.pop("tool_audit", [])
        budget_state = data.pop("budget_state", {})
        legacy_tool_usage = data.pop("tool_usage", None)
        fixed: Dict[str, Any] = {
            "request_id": str(data.pop("request_id", "") or "").strip(),
            "course_name": str(data.pop("course_name", "") or "").strip(),
            "mode": str(data.pop("mode", "") or "").strip(),
            "user_id": str(data.pop("user_id", "default") or "default").strip() or "default",
            "trace_id": str(data.pop("trace_id", "") or "").strip(),
            "budget_state": dict(budget_state) if isinstance(budget_state, Mapping) else {},
            "tool_audit": [dict(x) for x in tool_audit if isinstance(x, Mapping)] if isinstance(tool_audit, list) else [],
            "idempotency_namespace": str(data.pop("idempotency_namespace", "") or "").strip(),
        }
        if isinstance(legacy_tool_usage, Mapping):
            fixed["budget_state"]["tool_usage"] = dict(legacy_tool_usage)
        return cls(**fixed, metadata=data)

    def to_dict(self) -> Dict[str, Any]:
        payload = {
            "request_id": self.request_id,
            "course_name": self.course_name,
            "mode": self.mode,
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "budget_state": self.budget_state,
            "tool_audit": self.tool_audit,
            "idempotency_namespace": self.idempotency_namespace,
        }
        payload.update(self.metadata)
        return payload

    def get(self, key: str, default: Any = None) -> Any:
        if key in _FIXED_FIELDS:
            return getattr(self, key, default)
        return self.metadata.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if key in _FIXED_FIELDS:
            setattr(self, key, value)
            return
        self.metadata[key] = value

    def setdefault(self, key: str, default: Any) -> Any:
        current = self.get(key, None)
        if current is not None:
            return current
        if key in _FIXED_FIELDS:
            setattr(self, key, default)
            return default
        return self.metadata.setdefault(key, default)


_active_request_context: contextvars.ContextVar[Optional[RequestContext]] = contextvars.ContextVar(
    "coursepilot_request_context",
    default=None,
)


def set_request_context(payload: RequestContext | Mapping[str, Any]) -> RequestContext:
    ctx = payload if isinstance(payload, RequestContext) else RequestContext.from_mapping(payload)
    _active_request_context.set(ctx)
    return ctx


def get_request_context() -> Optional[RequestContext]:
    return _active_request_context.get()


def clear_request_context() -> None:
    _active_request_context.set(None)
