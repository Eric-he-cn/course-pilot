"""Tutor Agent for learning mode."""
from typing import List, Optional
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import TUTOR_PROMPT
from mcp_tools.client import get_tool_schemas
from backend.schemas import RetrievedChunk, TutorResult


class TutorAgent:
    """Tutor agent for teaching and explaining concepts."""
    
    def __init__(self):
        self.llm = get_llm_client()
    
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
    ) -> TutorResult:
        """Generate teaching response, returns structured TutorResult.
        
        Args:
            system_prompt_override: 若提供，直接使用此 system prompt（跳过工具规则构建）
            user_content_override: 若提供，直接使用此内容而非 TUTOR_PROMPT
        """
        # 如果有 override，直接用；否则用 TUTOR_PROMPT
        if user_content_override:
            prompt = user_content_override
        else:
            prompt = TUTOR_PROMPT.format(
                course_name=course_name,
                context=context,
                question=question
            )

        # system prompt 构建逻辑
        if system_prompt_override:
            system_prompt = system_prompt_override
        elif allowed_tools:
            schemas = get_tool_schemas(allowed_tools)
            tool_desc = "、".join(allowed_tools)
            system_prompt = (
                f"你是一位专业的大学课程导师。"
                f"你可以使用以下工具：{tool_desc}。"
                f"规则：\n"
                f"1. 遇到数学计算，必须调用 calculator 工具，不要自己心算。\n"
                f"2. 优先从数据库中获取数据，遇到超出知识库的信息或者需要网络查询的信息，可以调用 websearch 工具。\n"
                f"3. 用户明确要求保存笔记，必须调用 filewriter 工具，文件名用中文，格式为 .md。\n"
                f"4. 用户要求生成思维导图或知识点汇总，必须调用 mindmap_generator 工具。\n"
                f"5. 如果该知识点用户之前问过或做错过，可以调用 memory_search 工具检索历史记录。\n"
                f"6. 遇到询问当前日期、时间、星期几等时效性问题，必须调用 get_datetime 工具，不得凭记忆或训练数据回答。\n"
                f"7. 禁止编造工具调用结果，必须等待工具真实返回后再回答。"
            )
        else:
            system_prompt = "你是一位专业的大学课程导师。"

        # 注入用户画像（薄弱知识点等），失败不影响主流程
        try:
            from memory.manager import get_memory_manager
            profile_ctx = get_memory_manager().get_profile_context(course_name)
            if profile_ctx:
                system_prompt += f"\n\n【用户学习档案】{profile_ctx}"
        except Exception:
            pass

        # 构建 messages：system + 历史轮次 + 当前问题
        messages: List[dict] = [{"role": "system", "content": system_prompt}]

        # 插入历史对话（最多保留最近 20 条，避免 token 超限）
        if history:
            for msg in history[-20:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": prompt})

        if allowed_tools:
            schemas = get_tool_schemas(allowed_tools)
            raw = self.llm.chat_with_tools(messages, tools=schemas,
                                           temperature=temperature, max_tokens=max_tokens)
            return TutorResult(content=raw)
        raw = self.llm.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return TutorResult(content=raw)

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
    ):
        """流式版本的 teach()，返回文本 chunk 生成器。"""
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
            schemas = get_tool_schemas(allowed_tools)
            tool_desc = "、".join(allowed_tools)
            system_prompt = (
                f"你是一位专业的大学课程导师。"
                f"你可以使用以下工具：{tool_desc}。"
                f"规则：\n"
                f"1. 遇到数学计算，必须调用 calculator 工具，不要自己心算。\n"
                f"2. 优先从数据库中获取数据，遇到超出知识库的信息或者需要网络查询的信息（新闻/网络资料/日期/天气等），可以调用 websearch 工具，但仍然以数据库为准。\n"
                f"3. 用户明确要求保存笔记，必须调用 filewriter 工具，文件名用中文，格式为 .md。\n"
                f"4. 用户要求生成思维导图或知识点汇总，必须调用 mindmap_generator 工具。\n"
                f"5. 如果该知识点用户之前问过或做错过，可以调用 memory_search 工具检索历史记录。\n"
                f"6. 遇到询问当前日期、时间、星期几等时效性问题，必须调用 get_datetime 工具，不得凭记忆或训练数据回答。\n"
                f"7. 禁止编造工具调用结果，必须等待工具真实返回后再回答。"
            )
        else:
            system_prompt = "你是一位专业的大学课程导师。"

        # 注入用户画像（薄弱知识点等），失败不影响主流程
        try:
            from memory.manager import get_memory_manager
            profile_ctx = get_memory_manager().get_profile_context(course_name)
            if profile_ctx:
                system_prompt += f"\n\n【用户学习档案】{profile_ctx}"
        except Exception:
            pass

        messages: List[dict] = [{"role": "system", "content": system_prompt}]
        if history:
            for msg in history[-20:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})

        if allowed_tools:
            schemas = get_tool_schemas(allowed_tools)
            yield from self.llm.chat_stream_with_tools(messages, tools=schemas,
                                                       temperature=temperature, max_tokens=max_tokens)
        else:
            yield from self.llm.chat_stream(messages, temperature=temperature, max_tokens=max_tokens)
