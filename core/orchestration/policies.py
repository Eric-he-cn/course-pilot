"""Tool policy for different modes."""
from typing import List, Literal

# 所有可用工具的完整列表
ALL_TOOLS = ["calculator", "websearch", "filewriter", "memory_search", "mindmap_generator", "get_datetime"]


class ToolPolicy:
    """Define tool access policy for different modes.

    三种模式的差异体现在 runner 的工作逻辑和 Prompt 上，而非工具白名单：
    - learn   : Tutor 主导，RAG 讲解 + ReAct 工具调用
    - practice: 对话式出题/评分，LLM 直接驱动
    - exam    : 严格三阶段考试流程，LLM 直接驱动

    工具白名单只限制 Agent，不限制用户——用户在任何模式下都可以自行查阅
    外部资料，因此按模式屏蔽工具没有实质意义，反而削弱了 Agent 的能力。
    """

    MODE_POLICIES = {
        "learn":    ALL_TOOLS,
        "practice": ALL_TOOLS,
        "exam":     ALL_TOOLS,
    }

    @staticmethod
    def get_allowed_tools(mode: Literal["learn", "practice", "exam"]) -> List[str]:
        """Get allowed tools for a mode."""
        return ToolPolicy.MODE_POLICIES.get(mode, ALL_TOOLS)

    @staticmethod
    def is_tool_allowed(tool: str, mode: Literal["learn", "practice", "exam"]) -> bool:
        """Check if a tool is allowed in a mode."""
        return tool in ToolPolicy.get_allowed_tools(mode)
