"""Core LLM client with OpenAI-compatible interface."""
import os
import json
import logging
from typing import List, Dict, Any, Optional
from time import perf_counter
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


class LLMClient:
    """OpenAI-compatible LLM client."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
    
    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> str:
        """Send chat completion request."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error calling LLM: {str(e)}"
    
    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> str:
        """带 Function Calling 的对话，支持多轮工具调用直到 LLM 停止请求工具。"""
        logger = logging.getLogger("llm.tools")
        from mcp_tools.client import MCPTools

        if not tools:
            return self.chat(messages, temperature, max_tokens)

        tool_names = [t["function"]["name"] for t in tools]
        logger.info("[tools] call.start tools=%s", tool_names)
        messages = list(messages)
        max_rounds = 6  # 最多 6 轮工具调用，防止死循环

        try:
            for round_idx in range(max_rounds):
                t_llm = perf_counter()
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                msg = response.choices[0].message

                # LLM 不再调用工具，返回最终答案
                if not msg.tool_calls:
                    logger.info(
                        "[tools] round=%d no_more_tool_calls llm_ms=%.1f",
                        round_idx + 1,
                        (perf_counter() - t_llm) * 1000,
                    )
                    return msg.content or ""

                requested = [tc.function.name for tc in msg.tool_calls]
                logger.info(
                    "[tools] round=%d requested=%s llm_ms=%.1f",
                    round_idx + 1,
                    requested,
                    (perf_counter() - t_llm) * 1000,
                )

                # 把 assistant 消息加入历史
                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                })

                # 执行每个工具并把结果加入历史
                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                    t_tool = perf_counter()
                    logger.debug("[tools] execute name=%s args=%s", tool_name, tool_args)
                    tool_result = MCPTools.call_tool(tool_name, **tool_args)
                    logger.info(
                        "[tools] executed name=%s success=%s elapsed_ms=%.1f",
                        tool_name,
                        bool(tool_result.get("success", False)),
                        (perf_counter() - t_tool) * 1000,
                    )
                    logger.debug("[tools] result name=%s body=%s", tool_name, str(tool_result)[:300])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result, ensure_ascii=False)
                    })

            # 超过最大轮数，做一次不带工具的最终调用
            logger.warning("[tools] max_rounds_reached=%d force_final_completion=1", max_rounds)
            final = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return final.choices[0].message.content or ""

        except Exception as e:
            logger.exception("[tools] call.error fallback_to_plain_chat=1")
            return self.chat(messages, temperature, max_tokens)

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        **kwargs
    ):
        """Send streaming chat completion request."""
        try:
            stream = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                stream=True,
                **kwargs
            )
            for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            yield f"Error calling LLM: {str(e)}"

    def chat_stream_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ):
        """工具调用（非流式）后，将最终回答流式输出，返回生成器。"""
        logger = logging.getLogger("llm.stream_tools")
        from mcp_tools.client import MCPTools

        if not tools:
            yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)
            return

        tool_names = [t["function"]["name"] for t in tools]
        logger.info("[stream_tools] call.start tools=%s", tool_names)
        messages = list(messages)
        max_rounds = 6

        try:
            for round_idx in range(max_rounds):
                t_llm = perf_counter()
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                msg = response.choices[0].message

                if not msg.tool_calls:
                    logger.info(
                        "[stream_tools] round=%d no_more_tool_calls llm_ms=%.1f start_stream=1",
                        round_idx + 1,
                        (perf_counter() - t_llm) * 1000,
                    )
                    # 工具调用结束，用当前 messages 做流式最终回答
                    yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)
                    return

                requested = [tc.function.name for tc in msg.tool_calls]
                logger.info(
                    "[stream_tools] round=%d requested=%s llm_ms=%.1f",
                    round_idx + 1,
                    requested,
                    (perf_counter() - t_llm) * 1000,
                )

                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                })

                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                    t_tool = perf_counter()
                    logger.debug("[stream_tools] execute name=%s args=%s", tool_name, tool_args)
                    tool_result = MCPTools.call_tool(tool_name, **tool_args)
                    logger.info(
                        "[stream_tools] executed name=%s success=%s elapsed_ms=%.1f",
                        tool_name,
                        bool(tool_result.get("success", False)),
                        (perf_counter() - t_tool) * 1000,
                    )
                    logger.debug("[stream_tools] result name=%s body=%s", tool_name, str(tool_result)[:300])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result, ensure_ascii=False)
                    })

            logger.warning("[stream_tools] max_rounds_reached=%d force_stream_final=1", max_rounds)
            yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)

        except Exception as e:
            logger.exception("[stream_tools] call.error fallback_to_stream_plain=1")
            yield f"（工具调用出错，降级回答）\n"
            yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)


# Global LLM client instance
_llm_client = None


def get_llm_client() -> LLMClient:
    """Get or create global LLM client."""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
