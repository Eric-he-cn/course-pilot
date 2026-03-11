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
from core.llm.openai_compat import get_llm_client
from core.orchestration.prompts import ROUTER_PROMPT
from core.orchestration.policies import ToolPolicy
from backend.schemas import Plan

"""
RouterAgent：把自然语言请求映射为可执行 Plan。
职责：聚合用户画像、调用路由提示词、解析模型输出并产出结构化计划。
"""
class RouterAgent:
    
    """初始化 RouterAgent，复用全局 LLM 客户端。"""
    def __init__(self):
        self.llm = get_llm_client()

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

    """规范化并修正重规划结果，确保工具权限和任务类型不会越权。"""
    @staticmethod
    def _normalize_plan(plan_dict: Dict[str, Any], mode: str) -> Plan:
        plan_dict = dict(plan_dict or {})
        plan_dict["allowed_tools"] = ToolPolicy.get_allowed_tools(mode)
        plan_dict["task_type"] = mode
        return Plan(**plan_dict)
    
    """生成 Router 执行计划：注入用户画像、调用模型、解析计划、失败兜底。"""
    def plan(
        self,
        user_message: str,
        mode: str,
        course_name: str
    ) -> Plan:
        # 1) 准备提示词上下文（含用户画像）
        weak_points_ctx = self._build_weak_points_ctx(course_name)

        # 2) 组装 Router 提示词
        prompt = ROUTER_PROMPT.format(
            mode=mode,
            course_name=course_name,
            user_message=user_message,
            weak_points_ctx=weak_points_ctx,
        )
        
        # 3) 调用模型生成规划
        messages = [
            {"role": "system", "content": "你是一个任务规划助手。"},
            {"role": "user", "content": prompt}
        ]
        response = self.llm.chat(messages, temperature=0.3)
        
        # 4) 解析模型输出并规范化字段
        try:
            plan_dict = self._extract_json_payload(response)
            return self._normalize_plan(plan_dict, mode)
        except Exception as e:
            print(f"Error parsing plan: {e}, using defaults")
            return self._build_default_plan(mode)

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
        prompt = f"""你是一个任务重规划助手，请基于失败原因修正执行计划。

课程模式: {mode}
课程名称: {course_name}
用户问题: {user_message}
失败原因: {reason}
上一版计划(JSON): {json.dumps(previous_plan.model_dump(), ensure_ascii=False)}
{weak_points_ctx}

输出要求：
1. 仅输出 JSON，不要额外解释。
2. 字段必须包含: need_rag/style/output_format。
3. allowed_tools 与 task_type 不需要你填写，系统会覆盖为安全值。
4. 若失败原因是“检索为空/资料缺失”，请优先把 need_rag 设为 false，并给出更稳妥的 style。

JSON 示例：
{{
  "need_rag": true,
  "style": "step_by_step",
  "output_format": "answer"
}}
"""
        messages = [
            {"role": "system", "content": "你是一个稳健的任务重规划助手。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = self.llm.chat(messages, temperature=0.2)
            plan_dict = self._extract_json_payload(response)
            return self._normalize_plan(plan_dict, mode)
        except Exception as e:
            print(f"Error replanning: {e}, fallback to previous plan")
            return previous_plan
