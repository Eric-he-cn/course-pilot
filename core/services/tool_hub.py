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
from core.runtime.request_context import RequestContext
from mcp_tools.client import MCPTools


def _tool_failure_class(tool_result: Dict[str, Any]) -> str:
    if not isinstance(tool_result, dict):
        return "fatal_error"
    explicit = str(tool_result.get("failure_class", "") or "").strip().lower()
    if explicit in {"success", "retryable_error", "fatal_error", "denied"}:
        return explicit
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
    TOOL_GROUPS = {
        "calculator": {"calculator", "grading"},
        "get_datetime": {"utility", "teaching"},
        "memory_search": {"memory", "teaching", "grading"},
        "mindmap_generator": {"teaching"},
        "websearch": {"rag", "teaching", "generation"},
        "filewriter": {"teaching", "generation", "grading"},
    }
    TOOL_POLICY_PROFILES = {
        "learn_readonly": {
            "allowed_tools": {"calculator", "get_datetime", "memory_search", "mindmap_generator", "websearch"},
            "allowed_tool_groups": {"teaching", "rag", "memory", "utility", "calculator"},
            "network_permission": True,
            "filesystem_write_scope": "deny",
            "tool_budget": {"per_request_total": 6, "per_round": 3, "per_tool": {"websearch": 1, "memory_search": 2}},
        },
        "practice_generate": {
            "allowed_tools": {"calculator", "get_datetime", "memory_search", "mindmap_generator", "filewriter"},
            "allowed_tool_groups": {"generation", "teaching", "rag", "memory", "utility"},
            "network_permission": False,
            "filesystem_write_scope": "notes_only",
            "tool_budget": {"per_request_total": 6, "per_round": 3, "per_tool": {"filewriter": 1, "memory_search": 2}},
        },
        "grading_restricted": {
            "allowed_tools": {"calculator", "get_datetime", "memory_search", "filewriter"},
            "allowed_tool_groups": {"grading", "calculator", "memory", "utility"},
            "network_permission": False,
            "filesystem_write_scope": "notes_only",
            "tool_budget": {"per_request_total": 4, "per_round": 2, "per_tool": {"calculator": 4, "memory_search": 1, "filewriter": 1, "websearch": 0}},
        },
        "exam_locked": {
            "allowed_tools": {"calculator", "get_datetime", "memory_search", "mindmap_generator", "filewriter"},
            "allowed_tool_groups": {"generation", "grading", "calculator", "memory", "utility"},
            "network_permission": False,
            "filesystem_write_scope": "notes_only",
            "tool_budget": {"per_request_total": 4, "per_round": 2, "per_tool": {"calculator": 4, "memory_search": 1, "websearch": 0, "filewriter": 1}},
        },
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
        ctx = MCPTools.get_request_context()
        session_id = str(ctx.get("session_id", "") or "").strip()
        taskgraph_step = str(ctx.get("taskgraph_step", "") or ctx.get("runtime_route", "") or "").strip()
        namespace = str(ctx.idempotency_namespace or "").strip()
        if namespace:
            return f"{tool_name}:{namespace}:{signature}"
        if session_id or taskgraph_step:
            return f"{tool_name}:{session_id}:{taskgraph_step}:{signature}"
        return f"{tool_name}:{signature}"

    @staticmethod
    def _runtime_context() -> RequestContext:
        return MCPTools.get_request_context()

    @classmethod
    def _tool_policy_profile(cls) -> str:
        ctx = cls._runtime_context()
        profile = str(ctx.tool_policy_profile or ctx.get("tool_policy_profile", "") or "").strip()
        if profile in cls.TOOL_POLICY_PROFILES:
            return profile
        mode = str(ctx.mode or ctx.get("mode", "") or "learn").strip().lower()
        return {
            "practice": "practice_generate",
            "exam": "exam_locked",
        }.get(mode, "learn_readonly")

    @classmethod
    def _profile_config(cls) -> Dict[str, Any]:
        return dict(cls.TOOL_POLICY_PROFILES.get(cls._tool_policy_profile(), cls.TOOL_POLICY_PROFILES["learn_readonly"]))

    @classmethod
    def _profile_denied_reason(cls, tool_name: str) -> str:
        profile = cls._tool_policy_profile()
        if tool_name == "websearch" and profile in {"grading_restricted", "exam_locked", "practice_generate"}:
            return "network_denied"
        if tool_name == "filewriter":
            return "filesystem_scope_denied"
        return "tool_policy_denied"

    @classmethod
    def _profile_allows(cls, tool_name: str) -> bool:
        config = cls._profile_config()
        allowed_tools = {str(x).strip() for x in config.get("allowed_tools", set())}
        if allowed_tools and tool_name not in allowed_tools:
            return False
        if tool_name == "websearch" and not bool(config.get("network_permission", False)):
            return False
        return True

    @classmethod
    def _notes_only_allows(cls, tool_args: Dict[str, Any]) -> bool:
        notes_dir = os.path.abspath(str(cls._runtime_context().get("notes_dir", "./data/notes") or "./data/notes"))
        filename = os.path.basename(str(tool_args.get("filename", "") or "").strip())
        if not filename:
            return False
        try:
            target_path = os.path.abspath(os.path.join(notes_dir, filename))
            common = os.path.commonpath([notes_dir, target_path])
        except Exception:
            return False
        return common == notes_dir

    @classmethod
    def _filesystem_scope_allows(cls, tool_name: str, tool_args: Dict[str, Any]) -> bool:
        if tool_name != "filewriter":
            return True
        scope = str(cls._profile_config().get("filesystem_write_scope", "deny")).strip().lower()
        if scope == "notes_only":
            return cls._notes_only_allows(tool_args)
        return False

    @classmethod
    def _budget_limits(cls, tool_name: str) -> Dict[str, Optional[int]]:
        ctx = cls._runtime_context()
        raw_budget = ctx.get("tool_budget", {})
        budget = raw_budget if isinstance(raw_budget, dict) else {}
        profile_budget = cls._profile_config().get("tool_budget", {})
        if isinstance(profile_budget, dict):
            merged_budget = dict(profile_budget)
            if isinstance(budget, dict):
                for key, value in budget.items():
                    if key == "per_tool":
                        continue
                    if key in profile_budget:
                        try:
                            merged_budget[key] = min(int(profile_budget[key]), int(value))
                        except Exception:
                            merged_budget[key] = profile_budget[key]
                    else:
                        merged_budget[key] = value
                profile_per_tool = profile_budget.get("per_tool") if isinstance(profile_budget.get("per_tool"), dict) else {}
                request_per_tool = budget.get("per_tool") if isinstance(budget.get("per_tool"), dict) else {}
                merged_per_tool = dict(profile_per_tool)
                for name, value in request_per_tool.items():
                    if name in profile_per_tool:
                        try:
                            merged_per_tool[name] = min(int(profile_per_tool[name]), int(value))
                        except Exception:
                            merged_per_tool[name] = profile_per_tool[name]
                    else:
                        merged_per_tool[name] = value
                merged_budget["per_tool"] = merged_per_tool
            budget = merged_budget

        def _int_or_none(value: Any) -> Optional[int]:
            try:
                parsed = int(value)
            except Exception:
                return None
            return max(0, parsed)

        per_tool_raw = budget.get("per_tool")
        per_tool_map = per_tool_raw if isinstance(per_tool_raw, dict) else {}
        per_tool_limit = _int_or_none(per_tool_map.get(tool_name, budget.get(tool_name)))
        return {
            "per_request_total": _int_or_none(budget.get("per_request_total", os.getenv("ACT_MAX_TOOLS_PER_REQUEST", ""))),
            "per_round": _int_or_none(budget.get("per_round", os.getenv("ACT_MAX_TOOLS_PER_ROUND", ""))),
            "per_tool": per_tool_limit,
        }

    @classmethod
    def _usage_state(cls) -> Dict[str, Any]:
        ctx = cls._runtime_context()
        usage = ctx.budget_state.setdefault(
            "tool_usage",
            {
                "executed_total": 0,
                "per_tool": {},
                "per_round": {},
            },
        )
        if not isinstance(usage, dict):
            usage = {"executed_total": 0, "per_tool": {}, "per_round": {}}
            ctx.budget_state["tool_usage"] = usage
        usage.setdefault("per_tool", {})
        usage.setdefault("per_round", {})
        return usage

    @classmethod
    def _group_allows(cls, tool_name: str) -> bool:
        ctx = cls._runtime_context()
        raw = ctx.get("allowed_tool_groups", [])
        allowed = {str(x).strip().lower() for x in raw if str(x).strip()} if isinstance(raw, list) else set()
        profile_groups = cls._profile_config().get("allowed_tool_groups", set())
        if isinstance(profile_groups, (list, set, tuple)):
            allowed = allowed.intersection({str(x).strip().lower() for x in profile_groups if str(x).strip()}) if allowed else {str(x).strip().lower() for x in profile_groups if str(x).strip()}
        if not allowed:
            return True
        tool_groups = {str(x).strip().lower() for x in cls.TOOL_GROUPS.get(tool_name, set())}
        if not tool_groups:
            return False
        return bool(tool_groups.intersection(allowed))

    @classmethod
    def budget_snapshot(cls, tool_round: int) -> Dict[str, Any]:
        usage = cls._usage_state()
        limits = cls._budget_limits("__all__")
        per_tool_limits = {}
        for name in cls.TOOL_PERMISSION.keys():
            per_tool_limits[name] = cls._budget_limits(name).get("per_tool")
        executed_total = int(usage.get("executed_total", 0) or 0)
        round_key = str(tool_round)
        round_used = int((usage.get("per_round", {}) or {}).get(round_key, 0) or 0)
        per_tool_used = {str(k): int(v or 0) for k, v in dict(usage.get("per_tool", {}) or {}).items()}

        def _remaining(limit: Any, used: int) -> Optional[int]:
            if limit is None:
                return None
            try:
                return max(0, int(limit) - int(used))
            except Exception:
                return None

        per_tool_remaining = {
            name: _remaining(per_tool_limits.get(name), int(per_tool_used.get(name, 0) or 0))
            for name in per_tool_limits.keys()
        }
        return {
            "limits": {
                "per_request_total": limits.get("per_request_total"),
                "per_round": limits.get("per_round"),
                "per_tool": per_tool_limits,
            },
            "usage": {
                "executed_total": executed_total,
                "current_round": tool_round,
                "current_round_used": round_used,
                "per_tool_used": per_tool_used,
            },
            "remaining": {
                "per_request_total": _remaining(limits.get("per_request_total"), executed_total),
                "per_round": _remaining(limits.get("per_round"), round_used),
                "per_tool": per_tool_remaining,
            },
        }

    @classmethod
    def _increment_usage(cls, tool_name: str, tool_round: int) -> None:
        usage = cls._usage_state()
        usage["executed_total"] = int(usage.get("executed_total", 0) or 0) + 1
        per_tool = usage.get("per_tool", {})
        per_tool[tool_name] = int(per_tool.get(tool_name, 0) or 0) + 1
        usage["per_tool"] = per_tool
        per_round = usage.get("per_round", {})
        round_key = str(tool_round)
        per_round[round_key] = int(per_round.get(round_key, 0) or 0) + 1
        usage["per_round"] = per_round

    def _cap_hit_decision(
        self,
        *,
        tool_name: str,
        tool_args: Dict[str, Any],
        permission_mode: str,
        reason: str,
    ) -> ToolDecision:
        signature = ToolPolicy.normalized_tool_signature(tool_name, tool_args)
        return ToolDecision(
            tool_name=tool_name,
            allowed=False,
            reason=reason,
            signature=signature,
            permission_mode=permission_mode,  # type: ignore[arg-type]
            idempotency_key=self._idempotency_key(tool_name, signature),
        )

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
        if not self._profile_allows(tool_name):
            signature = ToolPolicy.normalized_tool_signature(tool_name, tool_args)
            return ToolDecision(
                tool_name=tool_name,
                allowed=False,
                reason=self._profile_denied_reason(tool_name),
                signature=signature,
                permission_mode=permission_mode,  # type: ignore[arg-type]
                idempotency_key=self._idempotency_key(tool_name, signature),
            )
        if not self._filesystem_scope_allows(tool_name, tool_args):
            signature = ToolPolicy.normalized_tool_signature(tool_name, tool_args)
            return ToolDecision(
                tool_name=tool_name,
                allowed=False,
                reason="filesystem_scope_denied",
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
        ctx = self._runtime_context()
        if not str(ctx.mode or "").strip():
            ctx.mode = str(mode or "").strip()
        if not str(ctx.get("permission_mode", "") or "").strip():
            ctx.set("permission_mode", permission_mode)
        decision = self.decide(
            tool_name=tool_name,
            tool_args=tool_args,
            mode=mode,
            phase=phase,
            permission_mode=permission_mode,
            original_user_content=original_user_content,
        )
        budget_snapshot = self.budget_snapshot(tool_round=tool_round)
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
                failure_class="denied",
                denied_reason=decision.reason,
                metadata={
                    "tool_policy_profile": self._tool_policy_profile(),
                    "tool_budget_snapshot": budget_snapshot,
                },
            )
            self._append_audit(record)
            raise ToolDeniedError(decision.reason)
        if decision.allowed and not self._group_allows(tool_name):
            add_event("tool_group_denied_count", tool_name=tool_name, tool_round=tool_round)
            denied = decision.model_copy(update={"allowed": False, "reason": "tool_group_denied"})
            self._append_audit(
                ToolAuditRecord(
                    tool_name=tool_name,
                    signature=denied.signature,
                    permission_mode=permission_mode,  # type: ignore[arg-type]
                    allowed=False,
                    reason="tool_group_denied",
                    idempotency_key=denied.idempotency_key,
                    failure_class="denied",
                    denied_reason="tool_group_denied",
                    metadata={
                        "tool_round": tool_round,
                        "allowed_tool_groups": list(self._runtime_context().get("allowed_tool_groups", []) or []),
                        "tool_groups": sorted(list(self.TOOL_GROUPS.get(tool_name, set()))),
                        "tool_budget_snapshot": budget_snapshot,
                    },
                )
            )
            raise ToolDeniedError("tool_group_denied")
        limits = self._budget_limits(tool_name)
        usage = self._usage_state()
        current_total = int(usage.get("executed_total", 0) or 0)
        current_tool = int((usage.get("per_tool", {}) or {}).get(tool_name, 0) or 0)
        current_round = int((usage.get("per_round", {}) or {}).get(str(tool_round), 0) or 0)
        cap_reason = ""
        cap_event = ""
        if limits["per_request_total"] is not None and current_total >= int(limits["per_request_total"]):
            cap_reason = "tool_request_total_cap"
            cap_event = "tool_total_cap_hit_count"
        elif limits["per_tool"] is not None and current_tool >= int(limits["per_tool"]):
            cap_reason = "tool_per_tool_cap"
            cap_event = "per_tool_cap_hit_count"
        elif limits["per_round"] is not None and current_round >= int(limits["per_round"]):
            cap_reason = "tool_per_round_cap"
            cap_event = "tool_round_cap_hit_count"
        if cap_reason:
            add_event(cap_event or cap_reason, tool_name=tool_name, tool_round=tool_round)
            denied = self._cap_hit_decision(
                tool_name=tool_name,
                tool_args=tool_args,
                permission_mode=permission_mode,
                reason=cap_reason,
            )
            self._append_audit(
                ToolAuditRecord(
                    tool_name=tool_name,
                    signature=denied.signature,
                    permission_mode=permission_mode,  # type: ignore[arg-type]
                    allowed=False,
                    reason=cap_reason,
                    idempotency_key=denied.idempotency_key,
                    failure_class="denied",
                    denied_reason=cap_reason,
                    metadata={
                        "tool_round": tool_round,
                        "current_total": current_total,
                        "current_tool": current_tool,
                        "current_round": current_round,
                        "limits": limits,
                        "tool_budget_snapshot": budget_snapshot,
                    },
                )
            )
            raise ToolDeniedError(cap_reason)
        add_event(
            "tool_gate_decision",
            tool_name=tool_name,
            phase=phase,
            tool_gate_decision=decision.allowed,
            tool_skip_reason=None,
            tool_signature=decision.signature,
            tool_round=tool_round,
        )

        dedup_reason = ""
        now_ms = perf_counter() * 1000.0
        if decision.signature in tool_cache:
            dedup_reason = "exact_match_cache"
        elif (
            tool_name == "memory_search"
            and decision.signature in last_exec_ms
            and (now_ms - float(last_exec_ms.get(decision.signature, 0.0))) < float(os.getenv("TOOL_DEDUP_MIN_INTERVAL_MS", "2000"))
        ):
            dedup_reason = "memory_search_min_interval"

        start = perf_counter()
        if dedup_reason:
            decision = decision.model_copy(update={"dedup_hit": True, "dedup_reason": dedup_reason})
            if dedup_reason == "memory_search_min_interval" and decision.signature not in tool_cache:
                result = {
                    "tool": tool_name,
                    "query": str(tool_args.get("query", "") or ""),
                    "results": [],
                    "success": True,
                    "message": "memory_search deduped by min interval",
                    "via": "toolhub_dedup",
                    "failure_class": "success",
                    "denied_reason": "",
                }
            else:
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
            self._increment_usage(tool_name, tool_round)
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
            result.setdefault("denied_reason", "")
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
            denied_reason=str(result.get("denied_reason", "")) if isinstance(result, dict) else "",
            via=via,
            elapsed_ms=(perf_counter() - start) * 1000.0,
            metadata={
                "args": ToolPolicy.normalized_tool_args(tool_name, tool_args),
                "session_id": str(self._runtime_context().get("session_id", "") or ""),
                "taskgraph_step": str(self._runtime_context().get("taskgraph_step", "") or ""),
                "tool_policy_profile": self._tool_policy_profile(),
                "tool_budget_snapshot": budget_snapshot,
            },
        )
        self._append_audit(record)
        return decision, result

    @staticmethod
    def _append_audit(record: ToolAuditRecord) -> None:
        ctx = MCPTools.get_request_context()
        ctx.tool_audit.append(record.model_dump())


_DEFAULT_TOOL_HUB = ToolHub()


def get_default_tool_hub() -> ToolHub:
    return _DEFAULT_TOOL_HUB
