"""
【模块说明】
- 主要作用：实现 RouterAgent，根据用户输入生成执行计划 Plan。
- 核心类：RouterAgent。
- 核心方法：plan（注入用户画像后生成 need_rag/style/allowed_tools 等决策）。
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
            allowed_tools = ToolPolicy.get_allowed_tools(mode)
            plan_dict["allowed_tools"] = allowed_tools
            plan_dict["task_type"] = mode
            return Plan(**plan_dict)
        except Exception as e:
            print(f"Error parsing plan: {e}, using defaults")
            return self._build_default_plan(mode)
