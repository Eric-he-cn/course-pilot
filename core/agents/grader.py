"""
【模块说明】
- 主要作用：实现 GraderAgent，负责练习评分与讲评。
- 核心类：GraderAgent。
- 核心方法：grade（非流式评分）、grade_practice_stream（流式评分）。
"""
import json
from typing import List, Optional
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import GRADER_PROMPT, GRADER_SYSTEM, GRADER_PRACTICE_PROMPT
from backend.schemas import GradeReport


# 只暴露 calculator 给 Grader，不需要其他工具
_CALCULATOR_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": (
                "精确计算数学表达式。评分时必须用本工具汇总各得分点分值，"
                "不得心算。示例：sum([20,15,10,0])、round(20+15+10+0, 1)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "合法的 Python 数学表达式，如 'sum([20,15,8])'",
                    }
                },
                "required": ["expression"],
            },
        },
    }
]


class GraderAgent:
    """评分 Agent：负责对答案打分并输出改进建议。"""

    def __init__(self):
        self.llm = get_llm_client()

    def grade(
        self,
        question: str,
        standard_answer: str,
        rubric: str,
        student_answer: str,
        course_name: Optional[str] = None,
        context: Optional[str] = None,          # 可选：RAG 教材上下文，用于反馈引用
    ) -> GradeReport:
        """进行非流式评分，要求通过 calculator 汇总分数。"""
        # 可选教材上下文
        rag_ctx = ""
        if context and context.strip():
            rag_ctx = f"\n\n【教材参考（可在反馈中引用）】\n{context.strip()[:800]}"

        prompt = GRADER_PROMPT.format(
            question=question,
            standard_answer=standard_answer,
            rubric=rubric,
            student_answer=student_answer,
            rag_ctx=rag_ctx,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一位公正的评分专家。"
                    "计算总分时必须调用 calculator 工具，不得自行心算，"
                    "调用完毕后再输出 JSON 结果。"
                ),
            },
            {"role": "user", "content": prompt},
        ]

        # 使用带工具调用的接口，让 LLM 用 calculator 汇总分值
        response = self.llm.chat_with_tools(
            messages, tools=_CALCULATOR_TOOL, temperature=0.2, max_tokens=1200
        )
        
        # 解析模型输出（优先解析 JSON 代码块）
        try:
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()
            else:
                json_str = response.strip()
            
            grade_dict = json.loads(json_str)
            report = GradeReport(
                score=float(grade_dict.get("score", 0)),
                feedback=grade_dict.get("feedback", ""),
                mistake_tags=grade_dict.get("mistake_tags", []),
                references=[]
            )
            # 写入记忆（course_name 可能为 None，此时跳过）
            if course_name:
                self._save_to_memory(
                    course_name=course_name,
                    question=question,
                    student_answer=student_answer,
                    score=report.score,
                    mistake_tags=report.mistake_tags,
                )
            return report
        except Exception as e:
            print(f"Error parsing grade: {e}")
            return GradeReport(
                score=0.0,
                feedback="评分时出错，请重试。",
                mistake_tags=[],
                references=[]
            )

    def _save_to_memory(
        self,
        course_name: str,
        question: str,
        student_answer: str,
        score: float,
        mistake_tags: List[str],
    ) -> None:
        """将练习结果写入情景记忆（错题重要性=0.9，正确=0.4）。"""
        try:
            from memory.manager import get_memory_manager
            mgr = get_memory_manager()
            is_mistake = score < 60
            importance = 0.9 if is_mistake else 0.4
            content = f"题目: {question[:200]}\n学生答案: {student_answer[:200]}\n得分: {score:.0f}"
            if mistake_tags:
                content += f"\n错误类型: {', '.join(mistake_tags)}"
            event_type = "mistake" if is_mistake else "practice"
            mgr.save_episode(
                course_name=course_name,
                event_type=event_type,
                content=content,
                importance=importance,
                metadata={"score": score, "tags": mistake_tags},
            )
            if mistake_tags and is_mistake:
                mgr.update_weak_points(course_name, mistake_tags)
            mgr.record_practice_result(course_name, score, is_mistake)
        except Exception as e:
            print(f"[Memory] 错题记忆写入失败（不影响评分）: {e}")

    def grade_practice_stream(
        self,
        quiz_content: str,
        student_answer: str,
        course_name: Optional[str] = None,
        history_ctx: str = "",
    ):
        """专用练习评卷流式方法。

        工作流：
          1. 逐题核对（必须原文引用标准答案和学生答案）
          2. 调用 calculator 工具汇总得分
          3. 输出最终评分结果 + 讲评

        Args:
            quiz_content: 本次练习题目原文（从对话历史提取）。
            student_answer: 本轮学生提交的答案原文。
            course_name: 课程名称，用于注入学习档案上下文。
            history_ctx: 历史错题上下文字符串（可选，追加到 system prompt）。
        """
        system_prompt = GRADER_SYSTEM
        if history_ctx:
            system_prompt += f"\n\n{history_ctx}"

        # 注入用户学习档案（薄弱知识点等）
        try:
            from memory.manager import get_memory_manager
            profile_ctx = get_memory_manager().get_profile_context(course_name)
            if profile_ctx:
                system_prompt += f"\n\n【用户学习档案】{profile_ctx}"
        except Exception:
            pass

        user_prompt = GRADER_PRACTICE_PROMPT.format(
            quiz_content=quiz_content.strip(),
            student_answer=student_answer.strip(),
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        print(f"[GraderAgent] 启动练习评卷 ReAct 流式（课程: {course_name}）")
        yield from self.llm.chat_stream_with_tools(
            messages,
            tools=_CALCULATOR_TOOL,
            temperature=0.1,       # 评卷要求确定性，低温度
            max_tokens=2500,
        )
