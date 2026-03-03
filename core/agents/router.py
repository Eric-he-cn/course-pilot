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


class RouterAgent:
    """路由 Agent：负责把自然语言请求映射为可执行计划。"""
    
    def __init__(self):
        self.llm = get_llm_client()
    
    def plan(
        self,
        user_message: str,
        mode: str,
        course_name: str
    ) -> Plan:
        """生成执行计划，并注入记忆画像以提升规划准确性。"""
        # 从记忆库拉取用户薄弱知识点，注入 Router prompt 辅助规划
        weak_points_ctx = ""
        try:
            from memory.manager import get_memory_manager
            profile = get_memory_manager().get_profile_context(course_name)
            if profile:
                weak_points_ctx = f"\n\n【用户学习档案（供规划参考）】\n{profile}"
        except Exception:
            pass

        prompt = ROUTER_PROMPT.format(
            mode=mode,
            course_name=course_name,
            user_message=user_message,
            weak_points_ctx=weak_points_ctx,
        )
        
        messages = [
            {"role": "system", "content": "你是一个任务规划助手。"},
            {"role": "user", "content": prompt}
        ]
        
        response = self.llm.chat(messages, temperature=0.3)
        
        # Parse response and create plan
        try:
            # Try to extract JSON from response
            if "```json" in response:
                json_str = response.split("```json")[1].split("```")[0].strip()
            elif "```" in response:
                json_str = response.split("```")[1].split("```")[0].strip()
            else:
                json_str = response.strip()
            
            plan_dict = json.loads(json_str)
            
            # Override with policy if needed
            allowed_tools = ToolPolicy.get_allowed_tools(mode)
            plan_dict["allowed_tools"] = allowed_tools
            plan_dict["task_type"] = mode
            
            return Plan(**plan_dict)
        except Exception as e:
            print(f"Error parsing plan: {e}, using defaults")
            # Return default plan
            return Plan(
                need_rag=True,
                allowed_tools=ToolPolicy.get_allowed_tools(mode),
                task_type=mode,
                style="step_by_step",
                output_format="answer"
            )
