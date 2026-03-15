"""Per-request in-memory metrics collector used by benchmark scripts."""

from __future__ import annotations

import contextvars
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


_active_trace: contextvars.ContextVar[Optional["MetricsTrace"]] = contextvars.ContextVar(
    "coursepilot_active_metrics_trace",
    default=None,
)


@dataclass
class MetricsTrace:
    """Container for one request trace and its structured events."""

    meta: Dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)
    events: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_event(self, event_type: str, **payload: Any) -> None:
        event = {
            "type": event_type,
            "ts_ms": time.time() * 1000.0,
            "trace_id": self.trace_id,
        }
        event.update(payload)
        with self._lock:
            event["seq"] = len(self.events) + 1
            self.events.append(event)


class _TraceScope:
    def __init__(self, meta: Optional[Dict[str, Any]] = None):
        self._meta = dict(meta or {})
        self._trace: Optional[MetricsTrace] = None
        self._token = None

    def __enter__(self) -> MetricsTrace:
        self._trace = MetricsTrace(meta=self._meta)
        self._token = _active_trace.set(self._trace)
        return self._trace

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            _active_trace.reset(self._token)
        return False


def trace_scope(meta: Optional[Dict[str, Any]] = None) -> _TraceScope:
    """Create a context manager that activates a metrics trace."""

    return _TraceScope(meta=meta)


def get_active_trace() -> Optional[MetricsTrace]:
    """Return active trace in current context, if any."""

    return _active_trace.get()


def add_event(event_type: str, **payload: Any) -> None:
    """Append an event to active trace. No-op if no active trace."""

    trace = get_active_trace()
    if trace is None:
        return
    trace.add_event(event_type, **payload)


def estimate_text_tokens(text: Any) -> int:
    """Cheap token estimator used when provider usage is unavailable."""

    if text is None:
        return 0
    s = str(text)
    if not s:
        return 0
    # Coarse estimate for mixed Chinese/English content.
    return max(1, int(math.ceil(len(s) / 3.2)))


def estimate_prompt_tokens(messages: Any) -> int:
    """Estimate prompt tokens for OpenAI-style messages."""

    if not isinstance(messages, list):
        return estimate_text_tokens(messages)
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            total += estimate_text_tokens(msg)
            continue
        total += 4  # role/format overhead
        total += estimate_text_tokens(msg.get("content", ""))
        tool_calls = msg.get("tool_calls")
        if tool_calls is not None:
            total += estimate_text_tokens(tool_calls)
    return max(1, total)

