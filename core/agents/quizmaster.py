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


class QuizMasterAgent:
    """出题 Agent：负责生成结构化练习题。"""
    
    def __init__(self):
        self.llm = get_llm_client()
    
    def generate_quiz(
        self,
        course_name: str,
        topic: str,
        difficulty: str,
        context: str
    ) -> Quiz:
        """生成练习题，生成前先检索历史薄弱点。"""
        # 预查询历史错题，优先针对薄弱知识点出题
        memory_ctx = ""
        try:
            from mcp_tools.client import MCPTools
            mem = MCPTools.call_tool("memory_search", query=topic, course_name=course_name)
            if mem.get("success") and mem.get("results"):
                snippets = [
                    r.get("content", "")[:150]
                    for r in mem["results"][:3]
                    if r.get("content")
                ]
                if snippets:
                    memory_ctx = "【历史错题/薄弱点参考】\n" + "\n".join(f"- {s}" for s in snippets)
        except Exception:
            pass

        prompt = QUIZMASTER_PROMPT.format(
            course_name=course_name,
            topic=topic,
            difficulty=difficulty,
            context=context,
            memory_ctx=memory_ctx,
        )
        
        messages = [
            {"role": "system", "content": "你是一位出题专家。"},
            {"role": "user", "content": prompt}
        ]
        
        response = self.llm.chat(messages, temperature=0.8, max_tokens=1000)
        
        # 解析模型输出
        try:
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()
            else:
                json_str = response.strip()
            
            quiz_dict = json.loads(json_str)
            return Quiz(**quiz_dict)
        except Exception as e:
            print(f"Error parsing quiz: {e}")
            # Return a default quiz
            return Quiz(
                question="生成题目时出错，请重试。",
                standard_answer="N/A",
                rubric="N/A",
                difficulty=difficulty,
                chapter=topic
            )
