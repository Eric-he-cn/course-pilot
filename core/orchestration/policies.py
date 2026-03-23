"""
【模块说明】
- 主要作用：定义不同模式下的工具策略与统一工具契约（capability + preflight）。
- 核心类：ToolPolicy、ToolCapability。
- 核心方法：get_allowed_tools、tool_preflight、normalized_tool_signature。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Tuple

# 所有可用工具的完整列表
ALL_TOOLS = ["calculator", "websearch", "filewriter", "memory_search", "mindmap_generator", "get_datetime"]
ACT_PHASES = {"act"}


@dataclass(frozen=True)
class ToolCapability:
    """统一工具能力契约。"""

    intent_types: Tuple[str, ...]
    required_args: Tuple[str, ...]
    phase_allow: Tuple[str, ...]
    retry_policy: str = "once"
    dedup_scope: str = "request"
    fallback_mode: str = "synthesize"


class ToolPolicy:
    """统一工具访问策略与调用前门控。"""

    MODE_POLICIES = {
        "learn": ALL_TOOLS,
        "practice": ALL_TOOLS,
        "exam": ALL_TOOLS,
    }

    CAPABILITIES: Dict[str, ToolCapability] = {
        "calculator": ToolCapability(
            intent_types=("math_compute", "grading"),
            required_args=("expression",),
            phase_allow=("act",),
            retry_policy="none",
        ),
        "websearch": ToolCapability(
            intent_types=("fresh_info", "outside_knowledge"),
            required_args=("query",),
            phase_allow=("act",),
            retry_policy="once",
        ),
        "filewriter": ToolCapability(
            intent_types=("persist_note",),
            required_args=("filename", "content"),
            phase_allow=("act",),
            retry_policy="none",
            dedup_scope="round",
        ),
        "memory_search": ToolCapability(
            intent_types=("history_lookup",),
            required_args=("query", "course_name"),
            phase_allow=("act",),
            retry_policy="none",
        ),
        "mindmap_generator": ToolCapability(
            intent_types=("diagram", "summarize"),
            required_args=("topic", "course_name"),
            phase_allow=("act",),
            retry_policy="none",
            dedup_scope="round",
        ),
        "get_datetime": ToolCapability(
            intent_types=("time_info",),
            required_args=(),
            phase_allow=("act",),
            retry_policy="none",
        ),
    }

    @staticmethod
    def get_allowed_tools(mode: Literal["learn", "practice", "exam"]) -> List[str]:
        return ToolPolicy.MODE_POLICIES.get(mode, ALL_TOOLS)

    @staticmethod
    def is_tool_allowed(tool: str, mode: Literal["learn", "practice", "exam"]) -> bool:
        return tool in ToolPolicy.get_allowed_tools(mode)

    @staticmethod
    def get_capability(tool_name: str) -> ToolCapability:
        return ToolPolicy.CAPABILITIES.get(
            tool_name,
            ToolCapability(
                intent_types=("general",),
                required_args=(),
                phase_allow=("act",),
                retry_policy="none",
                dedup_scope="request",
                fallback_mode="synthesize",
            ),
        )

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _normalize_memory_query(query: str) -> str:
        src = str(query or "").strip().lower()
        if not src:
            return ""
        toks = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", src)
        toks = list(dict.fromkeys(toks))[:12]
        return "|".join(sorted(toks)) or src[:80]

    @staticmethod
    def normalized_tool_args(tool_name: str, tool_args: Dict[str, Any]) -> Dict[str, Any]:
        args = dict(tool_args or {})
        out: Dict[str, Any] = {}
        for k in sorted(args.keys()):
            v = args.get(k)
            if v is None:
                continue
            if isinstance(v, str):
                v = ToolPolicy._normalize_text(v)
            elif isinstance(v, list):
                if all(isinstance(x, str) for x in v):
                    v = sorted({ToolPolicy._normalize_text(x) for x in v if ToolPolicy._normalize_text(x)})
            out[k] = v

        if tool_name == "memory_search":
            out["query"] = ToolPolicy._normalize_memory_query(str(args.get("query", "")))
            if "top_k" in out:
                try:
                    out["top_k"] = int(out["top_k"])
                except Exception:
                    out["top_k"] = 5
        return out

    @staticmethod
    def normalized_tool_signature(tool_name: str, tool_args: Dict[str, Any]) -> str:
        normalized = ToolPolicy.normalized_tool_args(tool_name, tool_args)
        return f"{tool_name}:{json.dumps(normalized, ensure_ascii=False, sort_keys=True)}"

    @staticmethod
    def tool_preflight(
        tool_name: str,
        tool_args: Dict[str, Any],
        *,
        mode: str,
        phase: str,
        memory_search_in_act_default: bool,
    ) -> Tuple[bool, str, ToolCapability, str]:
        cap = ToolPolicy.get_capability(tool_name)
        signature = ToolPolicy.normalized_tool_signature(tool_name, tool_args)

        if tool_name not in ToolPolicy.get_allowed_tools(mode):  # mode gate
            return False, "mode_not_allowed", cap, signature
        if phase not in set(cap.phase_allow):  # phase gate
            return False, "phase_not_allowed", cap, signature
        for required in cap.required_args:  # args gate
            raw = tool_args.get(required)
            if raw is None:
                return False, f"missing_required:{required}", cap, signature
            if isinstance(raw, str) and not raw.strip():
                return False, f"missing_required:{required}", cap, signature
        if tool_name == "memory_search" and not memory_search_in_act_default:
            return False, "memory_search_disabled_in_act", cap, signature
        return True, "allowed", cap, signature
