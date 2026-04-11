"""
【模块说明】
- 主要作用：实现 RouterAgent，根据用户输入生成执行计划 Plan。
- 核心类：RouterAgent。
- 核心方法：plan（注入用户画像后生成 need_rag/style/allowed_tools 等决策）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import json
from typing import Dict, Any
from backend.schemas import Plan, PlanPlusV1, SessionStateV1
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
    def __init__(self):
        super().__init__(agent_name="router")

    """提示词与解析辅助。"""

    """构建用户薄弱点上下文（供 Router 提示词注入）。失败时返回空字符串，不影响主流程。"""
    def _build_weak_points_ctx(self, course_name: str) -> str:
        try:
            from memory.manager import get_memory_manager
            profile = get_memory_manager().get_profile_context(course_name)
            if profile:
                return f"\n\n【用户学习档案（供规划参考）】\n{profile}"
        except Exception:
            pass
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
        return default

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
    def _normalize_plan(plan_dict: Dict[str, Any], mode: str, user_message: str) -> PlanPlusV1:
        plan_dict = dict(plan_dict or {})
        resolved_mode = RouterAgent._infer_resolved_mode(plan_dict, mode, user_message)
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
        return PlanPlusV1(**plan_dict)

    def build_context(
        self,
        session_state: SessionStateV1,
        *,
        course_name: str,
        user_message: str,
        mode_hint: str,
    ) -> Dict[str, Any]:
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
        return {
            "weak_points_ctx": weak_points_ctx,
            "session_ctx": session_ctx,
        }
    
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
            weak_points_ctx=f"{ctx['weak_points_ctx']}{ctx['session_ctx']}",
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
            return self._normalize_plan(plan_dict, mode, user_message)
        except Exception as e:
            print(f"Error parsing plan: {e}, using defaults")
            return self._normalize_plan(self._build_default_plan(mode).model_dump(), mode, user_message)

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
            return self._normalize_plan(plan_dict, mode, user_message)
        except Exception as e:
            print(f"Error replanning: {e}, fallback to previous plan")
            return previous_plan
