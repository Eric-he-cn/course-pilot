"""
【模块说明】
- 主要作用：实现 GraderAgent，负责练习评分与讲评。
- 核心类：GraderAgent。
- 核心方法：grade（非流式评分）、grade_practice_stream（流式评分）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import json
from typing import List, Optional
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import (
    GRADER_PROMPT,
    GRADER_GRADE_SYSTEM_PROMPT,
    GRADER_PRACTICE_PLAN_SYSTEM_PROMPT,
    GRADER_EXAM_PLAN_SYSTEM_PROMPT,
    GRADER_PRACTICE_PLAN_PROMPT,
    GRADER_EXAM_INTERNAL_PLAN_PROMPT,
    GRADER_SYSTEM,
    GRADER_PRACTICE_PROMPT,
    GRADER_EXAM_PROMPT,
)
from backend.schemas import GradeReport


"""只暴露 calculator 给 Grader，不需要其他工具。"""
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

"""GraderAgent：负责对答案评分、讲评，并按需写入记忆系统。"""
class GraderAgent:
    """评分 Agent 主体。"""

    """初始化 GraderAgent，复用全局 LLM 客户端。"""
    def __init__(self):
        self.llm = get_llm_client()

    """评分解析与消息组装辅助。"""

    """从模型输出中提取 JSON 负载。"""
    @staticmethod
    def _extract_json_payload(response_text: str) -> dict:
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()
        return json.loads(json_str)

    """把可选教材上下文压缩为评分提示词片段。"""
    @staticmethod
    def _build_rag_ctx(context: Optional[str]) -> str:
        if context and context.strip():
            return f"\n\n【教材参考（可在反馈中引用）】\n{context.strip()[:800]}"
        return ""

    """组装非流式评分消息列表。"""
    @staticmethod
    def _build_grade_messages(prompt: str) -> List[dict]:
        return [
            {
                "role": "system",
                "content": GRADER_GRADE_SYSTEM_PROMPT,
            },
            {"role": "user", "content": prompt},
        ]

    """构建练习评分“内部计划”提示词（仅供模型内部规划，不向用户展示）。"""
    @staticmethod
    def _build_practice_plan_prompt(quiz_content: str, student_answer: str) -> str:
        return GRADER_PRACTICE_PLAN_PROMPT.format(
            quiz_content=quiz_content,
            student_answer=student_answer,
        )

    """从题面文本中提取题号序列，供逐题计划兜底构建。"""
    @staticmethod
    def _extract_question_numbers(quiz_content: str) -> List[int]:
        import re

        numbers: List[int] = []
        seen = set()
        for match in re.finditer(r"(?:^|\n)\s*(?:第\s*(\d+)\s*题|(\d+)\s*[\.、:：)])", quiz_content or ""):
            raw = match.group(1) or match.group(2)
            if not raw:
                continue
            try:
                n = int(raw)
            except Exception:
                continue
            if n <= 0 or n in seen:
                continue
            seen.add(n)
            numbers.append(n)
        if not numbers:
            numbers = [1]
        return numbers

    """构建练习评分计划的本地兜底版本（每题一步 + 最后计算总分）。"""
    @classmethod
    def _build_default_practice_plan(cls, quiz_content: str) -> dict:
        q_numbers = cls._extract_question_numbers(quiz_content)
        question_steps = [
            {
                "question_no": n,
                "action": f"核对第{n}题标准答案与学生答案，给出该题得分与讲评",
            }
            for n in q_numbers
        ]
        score_formula = "sum([" + ",".join(f"q{n}_score" for n in q_numbers) + "])"
        return {
            "question_steps": question_steps,
            "final_step": "调用 calculator 汇总总分",
            "score_formula": score_formula,
            "checks": [f"核对第{n}题" for n in q_numbers],
            "key_mistakes": [],
            "question_count": len(q_numbers),
        }

    """生成评分内部计划；失败时返回可兜底的空计划。"""
    def _generate_practice_plan(self, quiz_content: str, student_answer: str) -> dict:
        default_plan = self._build_default_practice_plan(quiz_content)
        q_numbers = [item["question_no"] for item in default_plan["question_steps"]]
        messages = [
            {"role": "system", "content": GRADER_PRACTICE_PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_practice_plan_prompt(quiz_content, student_answer)},
        ]
        try:
            response = self.llm.chat(messages, temperature=0.1, max_tokens=800)
            plan = self._extract_json_payload(response)
            if not isinstance(plan, dict):
                return default_plan

            raw_steps = plan.get("question_steps", [])
            normalized_steps = []
            seen = set()
            if isinstance(raw_steps, list):
                for item in raw_steps:
                    if not isinstance(item, dict):
                        continue
                    try:
                        q_no = int(item.get("question_no", 0))
                    except Exception:
                        continue
                    if q_no not in q_numbers or q_no in seen:
                        continue
                    action = str(item.get("action", "")).strip() or f"核对第{q_no}题并给分"
                    normalized_steps.append({"question_no": q_no, "action": action[:100]})
                    seen.add(q_no)
            if len(normalized_steps) != len(q_numbers):
                normalized_steps = default_plan["question_steps"]

            final_step = str(plan.get("final_step", "")).strip() or default_plan["final_step"]
            score_formula = str(plan.get("score_formula", "")).strip()[:220]
            if not score_formula:
                score_formula = default_plan["score_formula"]

            checks = plan.get("checks", [])
            if not isinstance(checks, list):
                checks = default_plan["checks"]

            key_mistakes = plan.get("key_mistakes", [])
            if not isinstance(key_mistakes, list):
                key_mistakes = []

            return {
                "question_steps": normalized_steps,
                "final_step": final_step,
                "score_formula": score_formula,
                "checks": checks,
                "key_mistakes": key_mistakes,
                "question_count": len(normalized_steps),
            }
        except Exception:
            return default_plan

    """构建考试评分“内部计划”提示词（仅内部使用）。"""
    @staticmethod
    def _build_exam_plan_prompt(exam_paper: str, student_answer: str) -> str:
        return GRADER_EXAM_INTERNAL_PLAN_PROMPT.format(
            exam_paper=exam_paper,
            student_answer=student_answer,
        )

    """生成考试评分内部计划。"""
    def _generate_exam_plan(self, exam_paper: str, student_answer: str) -> dict:
        messages = [
            {"role": "system", "content": GRADER_EXAM_PLAN_SYSTEM_PROMPT},
            {"role": "user", "content": self._build_exam_plan_prompt(exam_paper, student_answer)},
        ]
        try:
            response = self.llm.chat(messages, temperature=0.1, max_tokens=900)
            plan = self._extract_json_payload(response)
            if not isinstance(plan, dict):
                return {"checks": [], "score_formula": "", "weak_points_hint": []}
            return {
                "checks": plan.get("checks", []) if isinstance(plan.get("checks", []), list) else [],
                "score_formula": str(plan.get("score_formula", ""))[:200],
                "weak_points_hint": plan.get("weak_points_hint", [])
                if isinstance(plan.get("weak_points_hint", []), list)
                else [],
            }
        except Exception:
            return {"checks": [], "score_formula": "", "weak_points_hint": []}

    """构建评分解析失败时的兜底报告。"""
    @staticmethod
    def _build_default_report() -> GradeReport:
        return GradeReport(
            score=0.0,
            feedback="评分时出错，请重试。",
            mistake_tags=[],
            references=[],
        )

    """进行非流式评分，要求通过 calculator 汇总分数。"""
    def grade(
        self,
        question: str,
        standard_answer: str,
        rubric: str,
        student_answer: str,
        course_name: Optional[str] = None,
        context: Optional[str] = None,          # 可选：RAG 教材上下文，用于反馈引用
    ) -> GradeReport:
        # 1) 组装提示词
        rag_ctx = self._build_rag_ctx(context)

        prompt = GRADER_PROMPT.format(
            question=question,
            standard_answer=standard_answer,
            rubric=rubric,
            student_answer=student_answer,
            rag_ctx=rag_ctx,
        )

        # 2) 组装消息并调用模型
        messages = self._build_grade_messages(prompt)

        response = self.llm.chat_with_tools(
            messages, tools=_CALCULATOR_TOOL, temperature=0.2, max_tokens=1200
        )
        
        # 3) 解析模型输出
        try:
            grade_dict = self._extract_json_payload(response)
            report = GradeReport(
                score=float(grade_dict.get("score", 0)),
                feedback=grade_dict.get("feedback", ""),
                mistake_tags=grade_dict.get("mistake_tags", []),
                references=[]
            )
            # 4) 写入记忆（course_name 为空时跳过）
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
            return self._build_default_report()

    """将练习结果写入情景记忆（错题重要性=0.9，正确=0.4）。"""
    def _save_to_memory(
        self,
        course_name: str,
        question: str,
        student_answer: str,
        score: float,
        mistake_tags: List[str],
    ) -> None:
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
                metadata={
                    "score": score,
                    "tags": mistake_tags,
                    "mode": "practice",
                    "agent": "grader",
                    "phase": "grade",
                },
            )
            if mistake_tags and is_mistake:
                mgr.update_weak_points(course_name, mistake_tags)
            mgr.record_practice_result(course_name, score, is_mistake)
        except Exception as e:
            print(f"[Memory] 错题记忆写入失败（不影响评分）: {e}")

    """
    专用练习评卷流式方法。

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
    def grade_practice_stream(
        self,
        quiz_content: str,
        student_answer: str,
        course_name: Optional[str] = None,
        history_ctx: str = "",
    ):
        # 0) 第一阶段：先生成内部评分计划（不对用户展示）
        practice_plan = self._generate_practice_plan(
            quiz_content=quiz_content,
            student_answer=student_answer,
        )

        # 1) 组装 system prompt（含历史错题上下文）
        system_prompt = GRADER_SYSTEM
        if history_ctx:
            system_prompt += f"\n\n{history_ctx}"
        system_prompt += (
            "\n\n【内部评分计划（不要在最终回答中复述本段原文）】\n"
            + json.dumps(practice_plan, ensure_ascii=False)
        )
        system_prompt += (
            "\n请严格按内部评分计划执行，再给出最终评分与讲评。"
            "\n执行硬约束："
            "\n1. 必须按 question_steps 的题号顺序逐题评分，每一步只评一道题。"
            "\n2. 所有题目评分完成后，最后一步再调用 calculator 汇总总分。"
            "\n3. 评分依据只能来自题目内标准答案，不得自行改写标准结论。"
        )

        # 2) 注入用户学习档案（失败不影响主流程）
        try:
            from memory.manager import get_memory_manager
            profile_ctx = get_memory_manager().get_profile_context(course_name)
            if profile_ctx:
                system_prompt += f"\n\n【用户学习档案】{profile_ctx}"
        except Exception:
            pass

        # 3) 组装消息并流式评分
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

    """
    考试评卷流式方法（评分 + 讲解）。

    工作流：
      1. 内部生成评卷计划（不展示）
      2. 逐题核对并调用 calculator 汇总总分
      3. 输出批改报告（含逐题详批与复习建议）
    """
    def grade_exam_stream(
        self,
        exam_paper: str,
        student_answer: str,
        course_name: Optional[str] = None,
        history_ctx: str = "",
    ):
        exam_plan = self._generate_exam_plan(exam_paper=exam_paper, student_answer=student_answer)

        system_prompt = GRADER_SYSTEM
        if history_ctx:
            system_prompt += f"\n\n{history_ctx}"
        system_prompt += (
            "\n\n【内部评卷计划（不要在最终回答中复述本段）】\n"
            + json.dumps(exam_plan, ensure_ascii=False)
        )
        system_prompt += "\n请严格按内部评卷计划执行，并输出完整批改讲解。"

        try:
            from memory.manager import get_memory_manager
            profile_ctx = get_memory_manager().get_profile_context(course_name)
            if profile_ctx:
                system_prompt += f"\n\n【用户学习档案】{profile_ctx}"
        except Exception:
            pass

        user_prompt = GRADER_EXAM_PROMPT.format(
            exam_paper=exam_paper.strip(),
            student_answer=student_answer.strip(),
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        print(f"[GraderAgent] 启动考试评卷 ReAct 流式（课程: {course_name}）")
        yield from self.llm.chat_stream_with_tools(
            messages,
            tools=_CALCULATOR_TOOL,
            temperature=0.1,
            max_tokens=3200,
        )
