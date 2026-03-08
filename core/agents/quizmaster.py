"""
【模块说明】
- 主要作用：实现 QuizMasterAgent，用于按主题和难度生成练习题。
- 核心类：QuizMasterAgent。
- 核心方法：generate_quiz（结合记忆检索结果出题）。
"""
import json
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import QUIZMASTER_PROMPT
from backend.schemas import Quiz

"""
QuizMasterAgent：按知识点与难度生成结构化题目。
职责：融合历史错题上下文、调用出题提示词、解析 JSON 题目输出。
"""
class QuizMasterAgent:
    
    """初始化 QuizMasterAgent，复用全局 LLM 客户端。"""
    def __init__(self):
        self.llm = get_llm_client()

    """提示词与解析辅助。"""

    """从模型输出中提取 JSON 负载，兼容 ```json``` 代码块与纯 JSON 文本。"""
    @staticmethod
    def _extract_json_payload(response_text: str) -> dict:
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()
        return json.loads(json_str)

    """解析失败时返回兜底题目，防止上层链路中断。"""
    @staticmethod
    def _build_default_quiz(topic: str, difficulty: str) -> Quiz:
        return Quiz(
            question="生成题目时出错，请重试。",
            standard_answer="N/A",
            rubric="N/A",
            difficulty=difficulty,
            chapter=topic,
        )

    """把 memory_search 结果转换为出题参考上下文，仅保留最多 3 条精简片段。"""
    @staticmethod
    def _build_memory_ctx(mem_result: dict) -> str:
        if not mem_result.get("success") or not mem_result.get("results"):
            return ""
        snippets = [
            r.get("content", "")[:150]
            for r in mem_result["results"][:3]
            if r.get("content")
        ]
        if not snippets:
            return ""
        return "【历史错题/薄弱点参考】\n" + "\n".join(f"- {s}" for s in snippets)
    
    """生成练习题主入口：拉取记忆、组装提示词、调用模型并解析 JSON。"""
    def generate_quiz(
        self,
        course_name: str,
        topic: str,
        difficulty: str,
        context: str
    ) -> Quiz:
        # 1) 预查询历史错题，优先针对薄弱知识点出题
        memory_ctx = ""
        try:
            from mcp_tools.client import MCPTools
            mem = MCPTools.call_tool("memory_search", query=topic, course_name=course_name)
            memory_ctx = self._build_memory_ctx(mem)
        except Exception:
            pass

        # 2) 组装提示词
        prompt = QUIZMASTER_PROMPT.format(
            course_name=course_name,
            topic=topic,
            difficulty=difficulty,
            context=context,
            memory_ctx=memory_ctx,
        )
        
        # 3) 调用模型
        messages = [
            {"role": "system", "content": "你是一位出题专家。"},
            {"role": "user", "content": prompt}
        ]
        response = self.llm.chat(messages, temperature=0.8, max_tokens=1000)
        
        # 4) 解析模型输出
        try:
            quiz_dict = self._extract_json_payload(response)
            return Quiz(**quiz_dict)
        except Exception as e:
            print(f"Error parsing quiz: {e}")
            return self._build_default_quiz(topic=topic, difficulty=difficulty)
