"""
【模块说明】
- 主要作用：实现 RouterAgent，根据用户输入生成执行计划 Plan。
- 核心类：RouterAgent。
- 核心方法：plan（注入用户画像后生成 need_rag/style/allowed_tools 等决策）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import json
import re
from typing import Dict, Any
from backend.schemas import AgentContextV1, Plan, PlanPlusV1, SessionStateV1
from core.agents.base import BaseAgent
from core.orchestration.prompts import (
    ROUTER_PROMPT,
    ROUTER_SYSTEM_PROMPT,
    ROUTER_REPLAN_PROMPT,
    ROUTER_REPLAN_SYSTEM_PROMPT,
)
from core.orchestration.policies import ToolPolicy

"""
RouterAgent：把自然语言请求映射为可执行 Plan。
职责：聚合用户画像、调用路由提示词、解析模型输出并产出结构化计划。
"""
class RouterAgent(BaseAgent):
    
    """初始化 RouterAgent，复用全局 LLM 客户端。"""
    def __init__(self, **services: Any):
        super().__init__(agent_name="router", **services)

    """提示词与解析辅助。"""

    """构建用户薄弱点上下文（供 Router 提示词注入）。失败时返回空字符串，不影响主流程。"""
    def _build_weak_points_ctx(self, course_name: str) -> str:
        if self.memory_service is None:
            return ""
        profile = self.memory_service.get_profile_context(course_name)
        if profile:
            return f"\n\n【用户学习档案（供规划参考）】\n{profile}"
        return ""

    """从模型输出中提取 JSON 对象，兼容 ```json```、`````` 和纯 JSON 形态。"""
    @staticmethod
    def _extract_json_payload(response_text: str) -> Dict[str, Any]:
        if "```json" in response_text:
            json_str = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            json_str = response_text.split("```")[1].split("```")[0].strip()
        else:
            json_str = response_text.strip()
        return json.loads(json_str)

    """解析失败时的兜底计划，保证编排链路继续执行。"""
    @staticmethod
    def _build_default_plan(mode: str) -> Plan:
        return Plan(
            need_rag=True,
            allowed_tools=ToolPolicy.get_allowed_tools(mode),
            task_type=mode,
            style="step_by_step",
            output_format="answer",
        )

    @staticmethod
    def _fallback_keywords(user_message: str) -> list[str]:
        import re

        text = str(user_message or "").lower()
        terms = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text)
        out = []
        seen = set()
        for term in terms:
            if term not in seen:
                out.append(term)
                seen.add(term)
            if len(out) >= 12:
                break
        return out

    @staticmethod
    def _normalize_mode(value: Any, default: str = "learn") -> str:
        raw = str(value or "").strip().lower()
        if raw in {"learn", "practice", "exam"}:
            return raw
        if raw == "general":
            return default
        return default

    @staticmethod
    def _is_answer_like(user_message: str) -> bool:
        text = str(user_message or "").strip()
        if not text:
            return False
        if any(marker in text for marker in ["我的答案", "答案如下", "提交答案", "答：", "交卷"]):
            return True
        if re.search(r"[1-9][.、：:]\s*[A-Za-z正确错误√×对错]", text):
            return True
        if len(re.findall(r"(?:第\d+题|^\d+[.、])", text, re.MULTILINE)) >= 2:
            return True
        normalized = text.lower()
        simple_answers = {
            "a", "b", "c", "d", "ab", "ac", "ad", "bc", "bd", "cd", "abcd",
            "对", "错", "正确", "错误", "true", "false", "√", "×",
        }
        if normalized in simple_answers:
            return True
        if len(text) <= 24 and not any(k in text for k in ["出题", "出卷", "练习", "考试", "讲解", "解释", "再来"]):
            return True
        return False

    @classmethod
    def _infer_workflow_template(
        cls,
        plan_dict: Dict[str, Any],
        mode: str,
        user_message: str,
        session_state: SessionStateV1,
        resolved_mode: str,
    ) -> str:
        text = str(user_message or "").strip().lower()
        wants_combo = any(sig in text for sig in ["先讲", "讲完", "讲解后", "学完后", "再出题", "然后出题", "最后出题"])
        answer_like = cls._is_answer_like(user_message)
        has_active_practice = bool(session_state.active_practice or session_state.last_quiz)
        has_active_exam = bool(session_state.active_exam or session_state.last_exam)
        explicit = str(plan_dict.get("workflow_template", "") or "").strip().lower()
        allowed = {
            "learn_only",
            "practice_only",
            "exam_only",
            "learn_then_practice",
            "practice_then_review",
            "exam_then_review",
        }

        if resolved_mode == "learn":
            if explicit in allowed and explicit != "practice_only":
                return explicit
            if wants_combo or ("练习" in text and any(sig in text for sig in ["讲", "解释", "分析", "总结"])):
                return "learn_then_practice"
            return "learn_only"
        if resolved_mode == "practice":
            if answer_like and has_active_practice:
                return "practice_then_review"
            if explicit in allowed:
                return explicit
            return "practice_only"
        if answer_like and has_active_exam:
            return "exam_then_review"
        if explicit in allowed:
            return explicit
        return "exam_only"

    @staticmethod
    def _workflow_action_kind(template: str) -> str:
        mapping = {
            "learn_only": "learn_explain",
            "practice_only": "practice_generate",
            "exam_only": "exam_generate",
            "learn_then_practice": "learn_then_practice",
            "practice_then_review": "practice_grade",
            "exam_then_review": "exam_grade",
        }
        return mapping.get(template, "learn_explain")

    @staticmethod
    def _required_artifact_kind(template: str) -> str:
        if template.startswith("practice_then"):
            return "practice"
        if template.startswith("exam_then"):
            return "exam"
        return "none"

    @staticmethod
    def _default_route_reason(mode_hint: str, resolved_mode: str, template: str) -> str:
        if template == "learn_then_practice":
            return f"根据用户意图，从 {mode_hint} 进入先讲解再练习的组合模板"
        if template == "practice_then_review":
            return "检测到练习答案提交意图，切换到练习评卷模板"
        if template == "exam_then_review":
            return "检测到考试答案提交意图，切换到考试评卷模板"
        if resolved_mode == mode_hint:
            return "沿用用户当前选择的模式与模板"
        return f"根据任务意图从 {mode_hint} 调整为 {resolved_mode}"

    @staticmethod
    def _default_tool_budget(template: str) -> Dict[str, int]:
        base = {"per_request_total": 6, "per_round": 3}
        if template in {"practice_then_review", "exam_then_review"}:
            return {**base, "calculator": 4, "memory_search": 1, "websearch": 0}
        if template == "learn_then_practice":
            return {**base, "memory_search": 2, "websearch": 1}
        if template == "learn_only":
            return {**base, "memory_search": 2, "websearch": 1}
        return {**base, "memory_search": 1, "websearch": 1}

    @staticmethod
    def _default_allowed_tool_groups(template: str) -> list[str]:
        if template in {"practice_then_review", "exam_then_review"}:
            return ["grading", "calculator"]
        if template in {"practice_only", "exam_only"}:
            return ["generation", "rag"]
        if template == "learn_then_practice":
            return ["teaching", "generation", "rag", "memory"]
        return ["teaching", "rag", "memory"]

    @classmethod
    def _infer_resolved_mode(cls, plan_dict: Dict[str, Any], mode: str, user_message: str) -> str:
        explicit = cls._normalize_mode(plan_dict.get("resolved_mode"), "")
        if explicit:
            return explicit
        task_type = cls._normalize_mode(plan_dict.get("task_type"), "")
        if task_type:
            return task_type

        text = str(user_message or "").strip().lower()
        exam_keywords = ("考试", "试卷", "出卷", "模拟考", "模拟考试", "交卷", "阅卷")
        practice_keywords = (
            "练习", "刷题", "出题", "出一道题", "出几道题",
            "选择题", "判断题", "填空题", "简答题", "计算题",
        )
        learn_keywords = ("讲解", "解释", "学习", "总结", "知识点", "思维导图", "是什么", "为什么")
        combo_keywords = ("先讲", "讲完", "讲解后", "学完后", "再出题", "然后出题", "最后出题")

        if (
            (any(k in text for k in combo_keywords) or ("再" in text and any(k in text for k in practice_keywords)))
            and any(k in text for k in learn_keywords)
            and any(k in text for k in practice_keywords)
        ):
            return "learn"

        if any(k in text for k in exam_keywords):
            return "exam"
        if any(k in text for k in practice_keywords):
            return "practice"
        if any(k in text for k in learn_keywords):
            return "learn"
        return cls._normalize_mode(mode, "learn")

    @staticmethod
    def _default_mode_reason(mode_hint: str, resolved_mode: str) -> str:
        if resolved_mode == mode_hint:
            return "沿用用户当前选择的模式"
        return f"根据任务意图从 {mode_hint} 调整为 {resolved_mode}"

    @staticmethod
    def _normalize_risk_level(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"low", "medium", "high"}:
            return raw
        return "medium"

    @staticmethod
    def _normalize_permission_mode(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"safe", "standard", "elevated"}:
            return raw
        return "standard"

    @staticmethod
    def _normalize_replan_policy(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"never", "once_on_failure"}:
            return raw
        return "once_on_failure"

    """规范化并修正重规划结果，确保工具权限和任务类型不会越权。"""
    @staticmethod
    def _normalize_plan(
        plan_dict: Dict[str, Any],
        mode: str,
        user_message: str,
        session_state: SessionStateV1 | None = None,
    ) -> PlanPlusV1:
        if session_state is None:
            normalized_mode = RouterAgent._normalize_mode(mode, "learn")
            session_state = SessionStateV1(
                session_id="router-normalize-bootstrap",
                course_name="",
                requested_mode_hint=normalized_mode,  # type: ignore[arg-type]
                resolved_mode=normalized_mode,  # type: ignore[arg-type]
                task_full_text=user_message,
                task_summary=user_message[:120],
            )
        plan_dict = dict(plan_dict or {})
        resolved_mode = RouterAgent._infer_resolved_mode(plan_dict, mode, user_message)
        workflow_template = RouterAgent._infer_workflow_template(
            plan_dict,
            mode,
            user_message,
            session_state,
            resolved_mode,
        )
        action_kind = str(plan_dict.get("action_kind", "") or RouterAgent._workflow_action_kind(workflow_template)).strip()
        required_artifact_kind = str(
            plan_dict.get("required_artifact_kind", "") or RouterAgent._required_artifact_kind(workflow_template)
        ).strip()
        plan_dict["allowed_tools"] = ToolPolicy.get_allowed_tools(resolved_mode)
        plan_dict["task_type"] = resolved_mode
        question_raw = str(plan_dict.get("question_raw", "") or user_message or "").strip()
        user_intent = str(plan_dict.get("user_intent", "") or question_raw).strip()
        retrieval_query = str(plan_dict.get("retrieval_query", "") or question_raw).strip()
        memory_query = str(plan_dict.get("memory_query", "") or retrieval_query or question_raw).strip()
        keywords = plan_dict.get("retrieval_keywords")
        if isinstance(keywords, list):
            retrieval_keywords = [str(x).strip() for x in keywords if str(x).strip()][:12]
        else:
            retrieval_keywords = []
        if not retrieval_keywords:
            retrieval_keywords = RouterAgent._fallback_keywords(retrieval_query or question_raw)
        plan_dict["question_raw"] = question_raw
        plan_dict["user_intent"] = user_intent
        plan_dict["retrieval_query"] = retrieval_query
        plan_dict["memory_query"] = memory_query
        plan_dict["retrieval_keywords"] = retrieval_keywords
        plan_dict["resolved_mode"] = resolved_mode
        plan_dict["mode_reason"] = str(plan_dict.get("mode_reason", "") or RouterAgent._default_mode_reason(mode, resolved_mode)).strip()
        plan_dict["need_memory"] = bool(plan_dict.get("need_memory", True))
        plan_dict["workflow_template"] = workflow_template
        plan_dict["action_kind"] = action_kind
        try:
            plan_dict["route_confidence"] = max(0.0, min(1.0, float(plan_dict.get("route_confidence", 0.85))))
        except Exception:
            plan_dict["route_confidence"] = 0.85
        plan_dict["route_reason"] = str(
            plan_dict.get("route_reason", "") or RouterAgent._default_route_reason(mode, resolved_mode, workflow_template)
        ).strip()
        plan_dict["required_artifact_kind"] = (
            required_artifact_kind if required_artifact_kind in {"none", "practice", "exam"} else "none"
        )
        capabilities = plan_dict.get("capabilities")
        if isinstance(capabilities, list):
            normalized_capabilities = [str(x).strip() for x in capabilities if str(x).strip()]
        else:
            normalized_capabilities = []
        if plan_dict.get("need_rag", True):
            normalized_capabilities.append("rag")
        if plan_dict["need_memory"]:
            normalized_capabilities.append("memory")
        normalized_capabilities.extend(plan_dict["allowed_tools"])
        plan_dict["capabilities"] = list(dict.fromkeys(normalized_capabilities))
        plan_dict["risk_level"] = RouterAgent._normalize_risk_level(plan_dict.get("risk_level"))
        plan_dict["permission_mode"] = RouterAgent._normalize_permission_mode(plan_dict.get("permission_mode"))
        plan_dict["replan_policy"] = RouterAgent._normalize_replan_policy(plan_dict.get("replan_policy"))
        tool_budget = plan_dict.get("tool_budget")
        plan_dict["tool_budget"] = tool_budget if isinstance(tool_budget, dict) else RouterAgent._default_tool_budget(workflow_template)
        tool_groups = plan_dict.get("allowed_tool_groups")
        if isinstance(tool_groups, list):
            plan_dict["allowed_tool_groups"] = [str(x).strip() for x in tool_groups if str(x).strip()]
        else:
            plan_dict["allowed_tool_groups"] = RouterAgent._default_allowed_tool_groups(workflow_template)
        return PlanPlusV1(**plan_dict)

    def build_context(
        self,
        session_state: SessionStateV1,
        *,
        course_name: str,
        user_message: str,
        mode_hint: str,
    ) -> AgentContextV1:
        weak_points_ctx = self._build_weak_points_ctx(course_name)
        session_summary = str(session_state.task_summary or "").strip()
        session_stage = str(session_state.current_stage or "").strip()
        session_ctx = ""
        if session_summary or session_stage:
            session_ctx = (
                "\n\n【当前会话状态】\n"
                f"- 当前阶段: {session_stage or 'router_planned'}\n"
                f"- 任务摘要: {session_summary or user_message[:120]}\n"
                f"- 模式提示: {mode_hint}"
            )
        return AgentContextV1(
            session_snapshot=session_state,
            merged_context=f"{weak_points_ctx}{session_ctx}",
            constraints={"mode_hint": mode_hint, "course_name": course_name},
            tool_scope={"permission_mode": session_state.permission_mode},
            metadata={
                "weak_points_ctx": weak_points_ctx,
                "session_ctx": session_ctx,
            },
        )
    
    """生成 Router 执行计划：注入用户画像、调用模型、解析计划、失败兜底。"""
    def plan(
        self,
        user_message: str,
        mode: str,
        course_name: str,
        session_state: SessionStateV1 = None,
    ) -> PlanPlusV1:
        if session_state is None:
            session_state = SessionStateV1(
                session_id="bootstrap",
                course_name=course_name,
                requested_mode_hint=self._normalize_mode(mode, "learn"),  # type: ignore[arg-type]
                resolved_mode=self._normalize_mode(mode, "learn"),  # type: ignore[arg-type]
                task_full_text=user_message,
                task_summary=user_message[:120],
            )
        # 1) 准备提示词上下文（含用户画像）
        ctx = self.build_context(
            session_state,
            course_name=course_name,
            user_message=user_message,
            mode_hint=mode,
        )

        # 2) 组装 Router 提示词
        prompt = ROUTER_PROMPT.format(
            mode=mode,
            course_name=course_name,
            user_message=user_message,
            weak_points_ctx=ctx.merged_context,
        )
        
        # 3) 调用模型生成规划
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ]
        response = self.invoke_llm(messages, temperature=0.3)
        
        # 4) 解析模型输出并规范化字段
        try:
            plan_dict = self._extract_json_payload(response)
            return self._normalize_plan(plan_dict, mode, user_message, session_state)
        except Exception as e:
            print(f"Error parsing plan: {e}, using defaults")
            return self._normalize_plan(self._build_default_plan(mode).model_dump(), mode, user_message, session_state)

    """重规划入口：当执行阶段发现质量/工具/检索异常时，基于失败原因生成一次替代计划。"""
    def replan(
        self,
        user_message: str,
        mode: str,
        course_name: str,
        previous_plan: Plan,
        reason: str,
    ) -> Plan:
        weak_points_ctx = self._build_weak_points_ctx(course_name)
        prompt = ROUTER_REPLAN_PROMPT.format(
            mode=mode,
            course_name=course_name,
            user_message=user_message,
            reason=reason,
            previous_plan_json=json.dumps(previous_plan.model_dump(), ensure_ascii=False),
            weak_points_ctx=weak_points_ctx,
        )
        messages = [
            {"role": "system", "content": ROUTER_REPLAN_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        try:
            response = self.invoke_llm(messages, temperature=0.2)
            plan_dict = self._extract_json_payload(response)
            bootstrap_state = SessionStateV1(
                session_id="replan",
                course_name=course_name,
                requested_mode_hint=self._normalize_mode(mode, "learn"),  # type: ignore[arg-type]
                resolved_mode=self._normalize_mode(mode, "learn"),  # type: ignore[arg-type]
                task_full_text=user_message,
                task_summary=user_message[:120],
            )
            return self._normalize_plan(plan_dict, mode, user_message, bootstrap_state)
        except Exception as e:
            print(f"Error replanning: {e}, fallback to previous plan")
            return previous_plan
