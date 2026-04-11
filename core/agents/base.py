"""
基础 Agent 抽象：统一会话态读取、上下文方法、LLM 调用与观测入口。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.schemas import AgentContextV1, AgentResultV1, SessionStateV1, StatePatchV1
from core.llm.openai_compat import get_llm_client
from core.metrics import add_event


class BaseAgent:
    """v3 Agent 基类。

    约定：
    - 每个 Agent 自己实现 build_context，体现差异化上下文策略。
    - SessionState 是 Agent 之间共享的短期状态载体。
    """

    agent_name = "base"

    def __init__(
        self,
        agent_name: Optional[str] = None,
        *,
        memory_service: Any = None,
        telemetry_service: Any = None,
        tool_hub: Any = None,
        event_bus: Any = None,
    ):
        self.agent_name = agent_name or self.agent_name
        self.llm = get_llm_client()
        self.logger = logging.getLogger(f"agent.{self.agent_name}")
        if memory_service is None or telemetry_service is None or tool_hub is None or event_bus is None:
            from core.services import get_default_event_bus, get_default_tool_hub
            from core.services.memory_service import MemoryService
            from core.services.telemetry_service import TelemetryService

            memory_service = memory_service or MemoryService()
            telemetry_service = telemetry_service or TelemetryService()
            tool_hub = tool_hub or get_default_tool_hub()
            event_bus = event_bus or get_default_event_bus()
        self.memory_service = memory_service
        self.telemetry_service = telemetry_service
        self.tool_hub = tool_hub
        self.event_bus = event_bus

    @staticmethod
    def load_session_state(session_state: Any) -> SessionStateV1:
        """兼容 dict / model 两种输入。"""

        if isinstance(session_state, SessionStateV1):
            return session_state
        if isinstance(session_state, dict):
            return SessionStateV1.model_validate(session_state)
        raise TypeError("session_state must be SessionStateV1 or dict")

    def build_context(
        self,
        session_state: SessionStateV1,
        **_: Any,
    ) -> AgentContextV1:
        """子类重写：按 Agent 角色构造上下文。"""

        return AgentContextV1(
            session_snapshot=session_state,
            merged_context=(
                f"task_full_text={session_state.task_full_text}\n"
                f"task_summary={session_state.task_summary}\n"
                f"current_stage={session_state.current_stage}"
            ).strip(),
            constraints={"agent": self.agent_name},
            tool_scope={"permission_mode": session_state.permission_mode},
        )

    def build_messages(self, *_: Any, **__: Any) -> List[Dict[str, Any]]:
        """子类按需重写；当前保留给后续 Runtime 统一入口。"""

        return []

    def invoke_llm(self, messages: List[Dict[str, Any]], **kwargs: Any) -> Any:
        """统一 LLM 调用入口，便于后续挂 trace / fallback。"""

        return self.llm.chat(messages, **kwargs)

    def extract_state_patch(self, *_: Any, **__: Any) -> StatePatchV1:
        """子类按需回写状态。默认无 patch。"""

        return StatePatchV1()

    def make_result(
        self,
        *,
        content: str = "",
        state_patch: Optional[StatePatchV1] = None,
        citations: Optional[List[Any]] = None,
        tool_calls_log: Optional[List[Any]] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> AgentResultV1:
        return AgentResultV1(
            content=content,
            state_patch=state_patch or StatePatchV1(),
            citations=citations or [],
            tool_calls_log=tool_calls_log or [],
            diagnostics=diagnostics or {},
        )

    def emit_telemetry(self, event_type: str, **payload: Any) -> None:
        add_event(event_type, agent=self.agent_name, **payload)

    def handle_fallback(self, reason: str, *, content: str = "") -> AgentResultV1:
        self.logger.warning("[fallback] agent=%s reason=%s", self.agent_name, reason)
        self.emit_telemetry("agent_fallback", reason=reason)
        patch = StatePatchV1(
            fallback_flags=[f"{self.agent_name}:{reason}"],
        )
        return self.make_result(content=content, state_patch=patch)

    def call_tool(
        self,
        tool_name: str,
        *,
        tool_args: Optional[Dict[str, Any]] = None,
        mode: str = "learn",
        phase: str = "act",
        permission_mode: str = "standard",
        original_user_content: str = "",
        tool_cache: Optional[Dict[str, Dict[str, Any]]] = None,
        last_exec_ms: Optional[Dict[str, float]] = None,
        tool_retry_max: int = 1,
        tool_round: int = 1,
    ) -> Dict[str, Any]:
        _, result = self.tool_hub.invoke(
            tool_name=tool_name,
            tool_args=dict(tool_args or {}),
            mode=mode,
            phase=phase,
            permission_mode=permission_mode,
            original_user_content=original_user_content,
            tool_cache=tool_cache or {},
            last_exec_ms=last_exec_ms or {},
            tool_retry_max=tool_retry_max,
            tool_round=tool_round,
        )
        return result
