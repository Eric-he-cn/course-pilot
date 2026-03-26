"""
【模块说明】
- 主要作用：实现 TutorAgent，负责学习/练习/考试场景下的教学回答生成。
- 核心类：TutorAgent。
- 核心方法：teach（非流式）、teach_stream（流式）、工具规则注入与用户画像注入。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
from typing import List, Optional
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import (
    TUTOR_PROMPT,
    TUTOR_DEFAULT_SYSTEM_PROMPT,
    TUTOR_TOOL_SYSTEM_PROMPT,
)
from mcp_tools.client import get_tool_schemas
from backend.schemas import TutorResult
from core.metrics import add_event, estimate_text_tokens

"""
TutorAgent：统一承载学习讲解、练习出题、考试对话等生成任务。
职责：组装消息、注入工具与用户画像规则、调用 LLM 返回文本。
"""
class TutorAgent:
    
    """初始化 TutorAgent，复用全局 LLM 客户端。"""
    def __init__(self):
        self.llm = get_llm_client()

    """统一组装消息列表（system + history + user），仅负责提示词与消息构造。"""
    def _build_messages(
        self,
        question: str,
        course_name: str,
        context: str,
        allowed_tools: Optional[List[str]] = None,
        history: Optional[List[dict]] = None,
        system_prompt_override: Optional[str] = None,
        user_content_override: Optional[str] = None,
        history_limit: int = 20,
        stream_mode: bool = False,
    ) -> List[dict]:
        if user_content_override:
            prompt = user_content_override
        else:
            prompt = TUTOR_PROMPT.format(
                course_name=course_name,
                context=context,
                question=question
            )

        if system_prompt_override:
            system_prompt = system_prompt_override
        elif allowed_tools:
            tool_desc = "、".join(allowed_tools)
            if stream_mode:
                rule_2 = (
                    "2. 优先从数据库中获取数据，遇到超出知识库的信息或者需要网络查询的信息"
                    "（新闻/网络资料/日期/天气等），可以调用 websearch 工具，但仍然以数据库为准。"
                )
            else:
                rule_2 = "2. 优先从数据库中获取数据，遇到超出知识库的信息或者需要网络查询的信息，可以调用 websearch 工具。"
            system_prompt = TUTOR_TOOL_SYSTEM_PROMPT.format(
                tool_desc=tool_desc,
                rule_2=rule_2,
            )
        else:
            system_prompt = TUTOR_DEFAULT_SYSTEM_PROMPT

        # 注入用户画像（薄弱知识点等），失败不影响主流程
        try:
            from memory.manager import get_memory_manager
            profile_ctx = get_memory_manager().get_profile_context(course_name)
            if profile_ctx:
                system_prompt += f"\n\n【用户学习档案】{profile_ctx}"
        except Exception:
            pass

        messages: List[dict] = [{"role": "system", "content": system_prompt}]
        include_raw_history = os.getenv("CB_INCLUDE_RAW_HISTORY_IN_MESSAGES", "0") == "1"
        try:
            recent_raw_turns = max(1, int(os.getenv("CB_RECENT_RAW_TURNS", "3")))
        except Exception:
            recent_raw_turns = 3
        effective_limit = min(history_limit, recent_raw_turns * 2)
        if history and include_raw_history:
            for msg in history[-effective_limit:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})
        add_event(
            "prompt_budget",
            system_tokens_est=estimate_text_tokens(system_prompt),
            history_tokens_est=estimate_text_tokens("\n".join(str(m.get("content", "")) for m in messages if m.get("role") in ("user", "assistant"))),
            context_tokens_est=estimate_text_tokens(context),
            history_msg_count=max(0, len(messages) - 2),
            include_raw_history_in_messages=include_raw_history,
            history_limit_applied=effective_limit if include_raw_history else 0,
            stream_mode=stream_mode,
        )
        return messages

    """根据 allowed_tools 解析工具 schema；返回 None 表示本轮不启用工具。"""
    @staticmethod
    def _resolve_tool_schemas(allowed_tools: Optional[List[str]]) -> Optional[List[dict]]:
        if not allowed_tools:
            return None
        return get_tool_schemas(allowed_tools)
    
    """非流式教学回答入口：组装消息后按工具配置调用模型并返回 TutorResult。"""
    def teach(
        self,
        question: str,
        course_name: str,
        context: str,
        allowed_tools: Optional[List[str]] = None,
        history: Optional[List[dict]] = None,
        system_prompt_override: Optional[str] = None,  # 练习/考试模式用此覆盖
        user_content_override: Optional[str] = None,   # 练习/考试模式用此覆盖
        temperature: float = 0.7,
        max_tokens: int = 2000,
        history_limit: int = 20,                        # 考试模式传 30
    ) -> TutorResult:
        # 1) 统一组装 messages
        messages = self._build_messages(
            question=question,
            course_name=course_name,
            context=context,
            allowed_tools=allowed_tools,
            history=history,
            system_prompt_override=system_prompt_override,
            user_content_override=user_content_override,
            history_limit=history_limit,
            stream_mode=False,
        )

        # 2) 根据是否启用工具调用不同 LLM 接口
        schemas = self._resolve_tool_schemas(allowed_tools)
        if schemas:
            raw = self.llm.chat_with_tools(messages, tools=schemas,
                                           temperature=temperature, max_tokens=max_tokens)
            return TutorResult(content=raw)
        raw = self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return TutorResult(content=raw)

    """流式教学回答入口：组装消息后按工具配置流式返回回答片段。"""
    def teach_stream(
        self,
        question: str,
        course_name: str,
        context: str,
        allowed_tools: Optional[List[str]] = None,
        history: Optional[List[dict]] = None,
        system_prompt_override: Optional[str] = None,
        user_content_override: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        history_limit: int = 20,                        # 考试模式传 30
    ):
        # 1) 统一组装 messages
        messages = self._build_messages(
            question=question,
            course_name=course_name,
            context=context,
            allowed_tools=allowed_tools,
            history=history,
            system_prompt_override=system_prompt_override,
            user_content_override=user_content_override,
            history_limit=history_limit,
            stream_mode=True,
        )

        # 2) 根据是否启用工具选择流式调用接口
        schemas = self._resolve_tool_schemas(allowed_tools)
        if schemas:
            yield from self.llm.chat_stream_with_tools(messages, tools=schemas,
                                                       temperature=temperature, max_tokens=max_tokens)
        else:
            yield from self.llm.chat_stream(messages, temperature=temperature, max_tokens=max_tokens)
