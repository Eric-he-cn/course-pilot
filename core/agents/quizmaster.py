"""
【模块说明】
- 主要作用：实现 QuizMasterAgent，用于按主题和难度生成练习题。
- 核心类：QuizMasterAgent。
- 核心方法：generate_quiz（结合记忆检索结果出题）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import json
from typing import Dict, Any
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import QUIZMASTER_PROMPT, EXAM_GENERATOR_PROMPT
from backend.schemas import Quiz
from mcp_tools.client import MCPTools

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
        snippets = []
        for r in mem_result["results"][:3]:
            text = ""
            if isinstance(r, dict):
                text = (
                    r.get("content")
                    or r.get("summary")
                    or r.get("text")
                    or ""
                )
            elif isinstance(r, str):
                text = r
            text = text.strip()
            if text:
                snippets.append(text[:150])
        if not snippets:
            return ""
        return "【历史错题/薄弱点参考】\n" + "\n".join(f"- {s}" for s in snippets)

    """规范化难度字段，兼容中文与大小写写法。"""
    @staticmethod
    def _normalize_difficulty(value: str, fallback: str = "medium") -> str:
        raw = (value or "").strip().lower()
        mapping = {
            "easy": "easy",
            "medium": "medium",
            "hard": "hard",
            "简单": "easy",
            "中等": "medium",
            "困难": "hard",
            "普通": "medium",
        }
        return mapping.get(raw, fallback if fallback in {"easy", "medium", "hard"} else "medium")

    """构建“出题计划”提示词（Plan 阶段）。"""
    @staticmethod
    def _build_plan_prompt(user_request: str, default_difficulty: str, memory_ctx: str) -> str:
        return f"""你是练习命题规划器。请根据用户请求先生成“出题计划”。

用户请求：{user_request}
默认难度：{default_difficulty}
{memory_ctx}

请只输出 JSON，字段如下：
{{
  "topic": "本次出题的核心知识点",
  "difficulty": "easy|medium|hard",
  "question_type": "选择题/判断题/填空题/简答题",
  "focus_points": ["知识点1", "知识点2"]
}}
"""

    """Plan 阶段：从用户请求中抽取主题与难度。"""
    def _plan_quiz(self, user_request: str, default_difficulty: str, memory_ctx: str) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": "你是一个严谨的出题规划器，只输出 JSON。"},
            {"role": "user", "content": self._build_plan_prompt(user_request, default_difficulty, memory_ctx)},
        ]
        try:
            response = self.llm.chat(messages, temperature=0.2, max_tokens=600)
            plan = self._extract_json_payload(response)
            if not isinstance(plan, dict):
                return {
                    "topic": user_request,
                    "difficulty": self._normalize_difficulty(default_difficulty),
                    "question_type": "简答题",
                    "focus_points": [],
                }
            return {
                "topic": str(plan.get("topic", user_request)).strip() or user_request,
                "difficulty": self._normalize_difficulty(
                    str(plan.get("difficulty", default_difficulty)),
                    self._normalize_difficulty(default_difficulty),
                ),
                "question_type": str(plan.get("question_type", "简答题")).strip() or "简答题",
                "focus_points": plan.get("focus_points", [])
                if isinstance(plan.get("focus_points", []), list)
                else [],
            }
        except Exception:
            return {
                "topic": user_request,
                "difficulty": self._normalize_difficulty(default_difficulty),
                "question_type": "简答题",
                "focus_points": [],
            }

    """根据请求判断是否需要时效类外部信息（避免无谓工具调用）。"""
    @staticmethod
    def _need_recent_web_info(text: str) -> bool:
        t = (text or "").lower()
        hints = [
            "近几年", "近年来", "最新", "最近", "今年", "时事", "高频考点", "趋势",
            "202", "news", "latest", "recent",
        ]
        return any(k in t for k in hints)

    """根据请求判断是否需要当前日期时间。"""
    @staticmethod
    def _need_datetime(text: str) -> bool:
        t = (text or "").lower()
        hints = ["今天", "当前时间", "日期", "截至", "now", "today", "date", "time"]
        return any(k in t for k in hints)

    """在必要时调用外部工具，生成附加上下文（每类工具最多一次）。"""
    def _build_external_ctx(self, query: str) -> str:
        parts = []
        if self._need_datetime(query):
            try:
                dt = MCPTools.call_tool("get_datetime")
                if dt.get("success"):
                    value = (
                        dt.get("datetime")
                        or dt.get("data", {}).get("datetime")
                        or dt.get("data", {}).get("iso")
                        or ""
                    )
                    if value:
                        parts.append(f"【当前时间】{value}")
            except Exception:
                pass

        if self._need_recent_web_info(query):
            try:
                ws = MCPTools.call_tool("websearch", query=query)
                if ws.get("success"):
                    data = ws.get("data", {}) if isinstance(ws.get("data"), dict) else {}
                    snippets = data.get("snippets", [])
                    if isinstance(snippets, list) and snippets:
                        picked = [str(x).strip() for x in snippets[:2] if str(x).strip()]
                        if picked:
                            parts.append("【网络参考】\n- " + "\n- ".join(p[:200] for p in picked))
            except Exception:
                pass

        return "\n".join(parts).strip()

    """构建考试计划提示词（Plan 阶段）。"""
    @staticmethod
    def _build_exam_plan_prompt(user_request: str, memory_ctx: str) -> str:
        return f"""你是考试命题规划器。请先生成一份考试出卷计划（JSON）。

用户请求：
{user_request}

{memory_ctx}

要求：
1. 只输出 JSON，不要解释。
2. 字段必须包含：
   - scope: 考试范围描述
   - num_questions: 题目总数（整数，建议 6~20）
   - difficulty_ratio: 题目难度分配（easy/medium/hard 的题目数量）
3. three 类题目数量之和必须等于 num_questions。

JSON 示例：
{{
  "scope": "第五章 Transformer",
  "num_questions": 10,
  "difficulty_ratio": {{"easy": 3, "medium": 5, "hard": 2}}
}}
"""

    """规范化考试计划，确保题量和难度分配合法。"""
    @staticmethod
    def _normalize_exam_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
        default = {"scope": "全部章节", "num_questions": 10, "difficulty_ratio": {"easy": 3, "medium": 5, "hard": 2}}
        if not isinstance(plan, dict):
            return default

        scope = str(plan.get("scope", default["scope"])).strip() or default["scope"]
        try:
            num_questions = int(plan.get("num_questions", default["num_questions"]))
        except Exception:
            num_questions = default["num_questions"]
        num_questions = max(3, min(30, num_questions))

        ratio_raw = plan.get("difficulty_ratio", {})
        if not isinstance(ratio_raw, dict):
            ratio_raw = {}
        easy = int(ratio_raw.get("easy", 0) or 0)
        medium = int(ratio_raw.get("medium", 0) or 0)
        hard = int(ratio_raw.get("hard", 0) or 0)

        if easy < 0:
            easy = 0
        if medium < 0:
            medium = 0
        if hard < 0:
            hard = 0

        total = easy + medium + hard
        if total <= 0:
            easy, medium, hard = 3, 5, 2
            total = 10

        # 按比例缩放到 num_questions，并修正四舍五入误差。
        easy_new = max(0, round(easy / total * num_questions))
        medium_new = max(0, round(medium / total * num_questions))
        hard_new = max(0, num_questions - easy_new - medium_new)
        if hard_new < 0:
            hard_new = 0
            medium_new = max(0, num_questions - easy_new)
        ratio = {"easy": easy_new, "medium": medium_new, "hard": hard_new}

        diff = num_questions - sum(ratio.values())
        if diff != 0:
            ratio["medium"] = max(0, ratio["medium"] + diff)

        return {"scope": scope, "num_questions": num_questions, "difficulty_ratio": ratio}

    """Plan 阶段：解析考试请求并生成结构化配置。"""
    def _plan_exam(self, user_request: str, memory_ctx: str) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": "你是一个严谨的考试命题规划器，只输出 JSON。"},
            {"role": "user", "content": self._build_exam_plan_prompt(user_request, memory_ctx)},
        ]
        try:
            response = self.llm.chat(messages, temperature=0.2, max_tokens=800)
            plan = self._extract_json_payload(response)
        except Exception:
            plan = {}
        return self._normalize_exam_plan(plan)

    """将考试 JSON 渲染为可展示试卷，并附隐藏答案元数据用于后续评分。"""
    @staticmethod
    def _render_exam_paper(course_name: str, exam_json: Dict[str, Any]) -> Dict[str, Any]:
        questions = exam_json.get("questions", []) if isinstance(exam_json, dict) else []
        if not isinstance(questions, list):
            questions = []

        title = str(exam_json.get("title", f"《{course_name}》模拟考试试卷"))
        instructions = str(exam_json.get("instructions", "请独立作答，完成后一次性提交全部答案。"))

        sections: Dict[str, list] = {}
        answer_sheet = []
        for idx, q in enumerate(questions, start=1):
            if not isinstance(q, dict):
                continue
            q_type = str(q.get("type", "综合题"))
            sections.setdefault(q_type, []).append((idx, q))
            answer_sheet.append(
                {
                    "id": idx,
                    "type": q_type,
                    "standard_answer": str(q.get("standard_answer", "")),
                    "rubric": str(q.get("rubric", "")),
                    "chapter": str(q.get("chapter", "")),
                    "concept": str(q.get("concept", "")),
                    "score": int(q.get("score", 0) or 0),
                }
            )

        lines = [
            f"# {title}",
            "",
            f"**考试须知**：{instructions}",
            "",
            "---",
            "",
        ]

        section_titles = ["第一部分", "第二部分", "第三部分", "第四部分", "第五部分"]
        for s_idx, (q_type, q_list) in enumerate(sections.items(), start=1):
            section_name = section_titles[s_idx - 1] if s_idx <= len(section_titles) else f"第{s_idx}部分"
            sec_score = sum(int(item.get("score", 0) or 0) for _, item in q_list)
            lines.append(f"## {section_name}　{q_type}（共{len(q_list)}题，共{sec_score}分）")
            lines.append("")
            for qid, q in q_list:
                q_score = int(q.get("score", 0) or 0)
                lines.append(f"{qid}. {q.get('question', '')}（{q_score}分）")
                options = q.get("options", [])
                if isinstance(options, list) and options:
                    lines.append("")
                    for opt in options:
                        lines.append(str(opt))
                lines.append("")

        lines.extend(
            [
                "---",
                "",
                "✅ 请将各题答案统一整理后一次性提交。",
                "",
            ]
        )
        return {
            "content": "\n".join(lines),
            "answer_sheet": answer_sheet,
            "total_score": sum(int(x.get("score", 0) or 0) for x in answer_sheet),
        }
    
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

        # 1.5) 先做内部计划（Plan），再按计划生成题目（Solve）
        quiz_plan = self._plan_quiz(
            user_request=topic,
            default_difficulty=difficulty,
            memory_ctx=memory_ctx,
        )
        planned_topic = quiz_plan["topic"]
        planned_difficulty = quiz_plan["difficulty"]

        # 2) 必要时补充外部上下文（单次调用，避免工具风暴）
        external_ctx = self._build_external_ctx(planned_topic)

        # 3) 组装提示词
        prompt = QUIZMASTER_PROMPT.format(
            course_name=course_name,
            topic=planned_topic,
            difficulty=planned_difficulty,
            context=context,
            memory_ctx=memory_ctx,
        )
        prompt += (
            "\n\n【内部出题计划（请执行但不要原样复述）】\n"
            + json.dumps(quiz_plan, ensure_ascii=False)
        )
        if external_ctx:
            prompt += "\n\n【必要外部参考（仅在确有必要时使用）】\n" + external_ctx
        prompt += (
            "\n\n工具使用约束：默认不需要任何外部工具。"
            "只有题目明确要求最新时效信息或当前时间时才使用外部参考。"
        )
        
        # 4) 调用模型（默认不开启 function-calling，避免非必要工具循环）
        messages = [
            {"role": "system", "content": "你是一位出题专家。"},
            {"role": "user", "content": prompt}
        ]
        response = self.llm.chat(messages, temperature=0.7, max_tokens=1200)
        
        # 5) 解析模型输出
        try:
            quiz_dict = self._extract_json_payload(response)
            quiz_dict["difficulty"] = self._normalize_difficulty(
                str(quiz_dict.get("difficulty", planned_difficulty)),
                planned_difficulty,
            )
            return Quiz(**quiz_dict)
        except Exception as e:
            print(f"Error parsing quiz: {e}")
            return self._build_default_quiz(topic=planned_topic, difficulty=planned_difficulty)

    """生成考试试卷主入口：Plan-Solve 生成结构化试卷并附隐藏答案。"""
    def generate_exam_paper(
        self,
        course_name: str,
        user_request: str,
        context: str,
    ) -> Dict[str, Any]:
        memory_ctx = ""
        try:
            from mcp_tools.client import MCPTools
            mem = MCPTools.call_tool("memory_search", query=user_request, course_name=course_name)
            memory_ctx = self._build_memory_ctx(mem)
        except Exception:
            pass

        exam_plan = self._plan_exam(user_request=user_request, memory_ctx=memory_ctx)
        external_ctx = self._build_external_ctx(user_request)
        prompt = EXAM_GENERATOR_PROMPT.format(
            course_name=course_name,
            num_questions=exam_plan["num_questions"],
            difficulty_ratio=exam_plan["difficulty_ratio"],
            context=context,
        )
        prompt += (
            "\n\n【内部考试计划（请执行但不要原样复述）】\n"
            + json.dumps(exam_plan, ensure_ascii=False)
            + "\n\n请仅输出 JSON，结构如下：\n"
            "{\n"
            '  "title": "《课程》模拟考试试卷",\n'
            '  "instructions": "考试须知",\n'
            '  "questions": [\n'
            "    {\n"
            '      "type": "单选题/判断题/简答题/计算题",\n'
            '      "question": "题干",\n'
            '      "options": ["A. ...", "B. ..."],\n'
            '      "score": 10,\n'
            '      "standard_answer": "标准答案",\n'
            '      "rubric": "评分标准",\n'
            '      "chapter": "章节",\n'
            '      "concept": "知识点"\n'
            "    }\n"
            "  ]\n"
            "}"
        )
        if external_ctx:
            prompt += "\n\n【必要外部参考（仅在确有必要时使用）】\n" + external_ctx
        prompt += "\n\n工具使用约束：默认不使用外部工具，仅在请求明确需要最新信息时使用外部参考。"

        messages = [
            {"role": "system", "content": "你是一位严谨的考试出题专家。"},
            {"role": "user", "content": prompt},
        ]
        response = self.llm.chat(messages, temperature=0.6, max_tokens=3000)

        try:
            exam_json = self._extract_json_payload(response)
            return self._render_exam_paper(course_name=course_name, exam_json=exam_json)
        except Exception:
            # 解析失败时退化为原始文本，至少保证可继续交互。
            return {
                "content": (
                    f"# 《{course_name}》模拟考试试卷\n\n"
                    "系统未能结构化解析试卷，以下为原始生成内容：\n\n"
                    + response
                ),
                "answer_sheet": [],
                "total_score": 0,
            }
