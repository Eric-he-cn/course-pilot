"""Unified ToolHub on top of ToolPolicy + MCP stdio."""

from __future__ import annotations

import json
import os
from time import perf_counter
from typing import Any, Dict, Optional, Tuple

from backend.schemas import ToolAuditRecord, ToolDecision
from core.errors import ToolDeniedError
from core.metrics import add_event
from core.orchestration.policies import ToolPolicy
from mcp_tools.client import MCPTools


def _tool_failure_class(tool_result: Dict[str, Any]) -> str:
    if not isinstance(tool_result, dict):
        return "fatal_error"
    if bool(tool_result.get("success", False)):
        return "success"
    err = str(tool_result.get("error", "")).lower()
    retryable_signals = (
        "timeout",
        "temporarily",
        "connection",
        "refused",
        "reset",
        "429",
        "rate limit",
        "unavailable",
        "network",
    )
    if any(sig in err for sig in retryable_signals):
        return "retryable_error"
    return "fatal_error"


class ToolHub:
    """Single tool execution entry with policy, dedup, idempotency, and audit."""

    PERMISSION_ORDER = {"safe": 0, "standard": 1, "elevated": 2}
    TOOL_PERMISSION = {
        "calculator": "safe",
        "get_datetime": "safe",
        "memory_search": "safe",
        "mindmap_generator": "standard",
        "websearch": "standard",
        "filewriter": "elevated",
    }

    @classmethod
    def _permission_allows(cls, tool_name: str, permission_mode: str) -> bool:
        required = cls.TOOL_PERMISSION.get(tool_name, "standard")
        current = permission_mode if permission_mode in cls.PERMISSION_ORDER else "standard"
        return cls.PERMISSION_ORDER[current] >= cls.PERMISSION_ORDER[required]

    @staticmethod
    def _allow_memory_in_act(original_user_content: str) -> bool:
        text = str(original_user_content or "").lower()
        if str(os.getenv("MEMORY_SEARCH_IN_ACT_DEFAULT", "0")).strip().lower() in {"1", "true", "yes", "on"}:
            return True
        signals = ("之前", "历史", "错题", "记忆", "复习", "上次", "薄弱点", "以前", "past", "history")
        return any(sig in text for sig in signals)

    @staticmethod
    def _idempotency_key(tool_name: str, signature: str) -> str:
        ctx = getattr(MCPTools, "_context", None)
        if isinstance(ctx, dict):
            session_id = str(ctx.get("session_id", "") or "").strip()
            taskgraph_step = str(ctx.get("taskgraph_step", "") or ctx.get("runtime_route", "") or "").strip()
            if session_id or taskgraph_step:
                return f"{tool_name}:{session_id}:{taskgraph_step}:{signature}"
        return f"{tool_name}:{signature}"

    def decide(
        self,
        *,
        tool_name: str,
        tool_args: Dict[str, Any],
        mode: str,
        phase: str,
        permission_mode: str,
        original_user_content: str,
    ) -> ToolDecision:
        if not self._permission_allows(tool_name, permission_mode):
            signature = ToolPolicy.normalized_tool_signature(tool_name, tool_args)
            return ToolDecision(
                tool_name=tool_name,
                allowed=False,
                reason="permission_denied",
                signature=signature,
                permission_mode=permission_mode,  # type: ignore[arg-type]
                idempotency_key=self._idempotency_key(tool_name, signature),
            )
        allowed, reason, _, signature = ToolPolicy.tool_preflight(
            tool_name=tool_name,
            tool_args=tool_args,
            mode=mode,
            phase=phase,
            memory_search_in_act_default=self._allow_memory_in_act(original_user_content),
        )
        return ToolDecision(
            tool_name=tool_name,
            allowed=bool(allowed),
            reason=reason,
            signature=signature,
            permission_mode=permission_mode,  # type: ignore[arg-type]
            idempotency_key=self._idempotency_key(tool_name, signature),
        )

    def invoke(
        self,
        *,
        tool_name: str,
        tool_args: Dict[str, Any],
        mode: str,
        phase: str,
        permission_mode: str,
        original_user_content: str,
        tool_cache: Dict[str, Dict[str, Any]],
        last_exec_ms: Dict[str, float],
        tool_retry_max: int,
        tool_round: int,
    ) -> Tuple[ToolDecision, Dict[str, Any]]:
        decision = self.decide(
            tool_name=tool_name,
            tool_args=tool_args,
            mode=mode,
            phase=phase,
            permission_mode=permission_mode,
            original_user_content=original_user_content,
        )
        add_event(
            "tool_gate_decision",
            tool_name=tool_name,
            phase=phase,
            tool_gate_decision=decision.allowed,
            tool_skip_reason=None if decision.allowed else decision.reason,
            tool_signature=decision.signature,
            tool_round=tool_round,
        )
        if not decision.allowed:
            add_event(
                "tool_skip",
                tool_name=tool_name,
                tool_skip_reason=decision.reason,
                tool_signature=decision.signature,
                tool_round=tool_round,
            )
            record = ToolAuditRecord(
                tool_name=tool_name,
                signature=decision.signature,
                permission_mode=permission_mode,  # type: ignore[arg-type]
                allowed=False,
                reason=decision.reason,
                idempotency_key=decision.idempotency_key,
            )
            self._append_audit(record)
            raise ToolDeniedError(decision.reason)

        dedup_reason = ""
        now_ms = perf_counter() * 1000.0
        if decision.signature in tool_cache:
            dedup_reason = "exact_match_cache"
        elif (
            tool_name == "memory_search"
            and decision.signature in last_exec_ms
            and (now_ms - float(last_exec_ms.get(decision.signature, 0.0))) < float(os.getenv("TOOL_DEDUP_MIN_INTERVAL_MS", "2000"))
            and decision.signature in tool_cache
        ):
            dedup_reason = "memory_search_min_interval"

        start = perf_counter()
        if dedup_reason:
            decision = decision.model_copy(update={"dedup_hit": True, "dedup_reason": dedup_reason})
            result = dict(tool_cache.get(decision.signature, {}))
            add_event(
                "tool_dedup",
                tool_name=tool_name,
                dedup_hit=True,
                dedup_reason=dedup_reason,
                tool_round=tool_round,
            )
        else:
            attempts = 0
            cap = ToolPolicy.get_capability(tool_name)
            max_attempts = max(1, 1 + min(tool_retry_max, 1 if cap.retry_policy == "once" else 0))
            result: Dict[str, Any] = {}
            failure_class = "fatal_error"
            while attempts < max_attempts:
                attempts += 1
                result = MCPTools.call_tool(tool_name, **tool_args)
                result = dict(result) if isinstance(result, dict) else {"result": str(result)}
                failure_class = _tool_failure_class(result)
                if failure_class != "retryable_error" or attempts >= max_attempts:
                    break
                add_event(
                    "tool_retry_count",
                    tool_name=tool_name,
                    tool_retry_count=attempts,
                    tool_failure_class=failure_class,
                    tool_round=tool_round,
                )
            result.setdefault("failure_class", failure_class)
            tool_cache[decision.signature] = dict(result)
            last_exec_ms[decision.signature] = perf_counter() * 1000.0
            add_event(
                "tool_dedup",
                tool_name=tool_name,
                dedup_hit=False,
                dedup_reason="executed",
                tool_round=tool_round,
            )
            add_event(
                "tool_failure_class",
                tool_name=tool_name,
                tool_failure_class=failure_class,
                tool_retry_count=max(0, attempts - 1),
                tool_round=tool_round,
            )

        via = str(result.get("via", "mcp_stdio")) if isinstance(result, dict) else "mcp_stdio"
        record = ToolAuditRecord(
            tool_name=tool_name,
            signature=decision.signature,
            permission_mode=permission_mode,  # type: ignore[arg-type]
            allowed=True,
            reason="allowed",
            success=bool(result.get("success", False)) if isinstance(result, dict) else False,
            dedup_hit=decision.dedup_hit,
            dedup_reason=decision.dedup_reason,
            idempotency_key=decision.idempotency_key,
            failure_class=str(result.get("failure_class", "")) if isinstance(result, dict) else "",
            via=via,
            elapsed_ms=(perf_counter() - start) * 1000.0,
            metadata={
                "args": ToolPolicy.normalized_tool_args(tool_name, tool_args),
                "session_id": str(getattr(MCPTools, "_context", {}).get("session_id", "") or ""),
                "taskgraph_step": str(getattr(MCPTools, "_context", {}).get("taskgraph_step", "") or ""),
            },
        )
        self._append_audit(record)
        return decision, result

    @staticmethod
    def _append_audit(record: ToolAuditRecord) -> None:
        ctx = getattr(MCPTools, "_context", None)
        if not isinstance(ctx, dict):
            return
        audit = ctx.setdefault("tool_audit", [])
        if isinstance(audit, list):
            audit.append(record.model_dump())


_DEFAULT_TOOL_HUB = ToolHub()


def get_default_tool_hub() -> ToolHub:
    return _DEFAULT_TOOL_HUB
