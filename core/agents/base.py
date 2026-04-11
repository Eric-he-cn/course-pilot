"""
基础 Agent 抽象：统一会话态读取、上下文方法、LLM 调用与观测入口。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.schemas import AgentResultV1, SessionStateV1, StatePatchV1
from core.llm.openai_compat import get_llm_client
from core.metrics import add_event


class BaseAgent:
    """v3 Agent 基类。

    约定：
    - 每个 Agent 自己实现 build_context，体现差异化上下文策略。
    - SessionState 是 Agent 之间共享的短期状态载体。
    """

    agent_name = "base"

    def __init__(self, agent_name: Optional[str] = None):
        self.agent_name = agent_name or self.agent_name
        self.llm = get_llm_client()
        self.logger = logging.getLogger(f"agent.{self.agent_name}")

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
    ) -> Dict[str, Any]:
        """子类重写：按 Agent 角色构造上下文。"""

        return {
            "task_full_text": session_state.task_full_text,
            "task_summary": session_state.task_summary,
            "current_stage": session_state.current_stage,
        }

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
