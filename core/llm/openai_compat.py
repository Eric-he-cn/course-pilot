"""
【模块说明】
- 主要作用：封装 OpenAI 兼容模型调用，提供普通对话与工具调用（含流式）。
- 核心类：LLMClient。
- 核心方法：chat/chat_stream、chat_with_tools/chat_stream_with_tools。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import json
import logging
from typing import List, Dict, Any, Optional
from time import perf_counter
from openai import OpenAI
from dotenv import load_dotenv
from core.metrics import add_event, estimate_prompt_tokens, get_active_trace

load_dotenv()


def _provider_from_base_url(base_url: str) -> str:
    u = (base_url or "").lower()
    if "deepseek" in u:
        return "deepseek"
    if "openai" in u:
        return "openai"
    if "ollama" in u:
        return "ollama"
    return "custom"


def _usage_tokens(response: Any) -> tuple[Optional[int], Optional[int]]:
    usage = None
    if isinstance(response, dict):
        usage = response.get("usage")
    else:
        usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
    else:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
    if isinstance(prompt_tokens, bool):
        prompt_tokens = None
    if isinstance(completion_tokens, bool):
        completion_tokens = None
    if isinstance(prompt_tokens, (int, float)):
        prompt_tokens = int(prompt_tokens)
    else:
        prompt_tokens = None
    if isinstance(completion_tokens, (int, float)):
        completion_tokens = int(completion_tokens)
    else:
        completion_tokens = None
    return prompt_tokens, completion_tokens


def _normalize_tool_args(tool_args: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(tool_args, dict):
        return {}
    out: Dict[str, Any] = {}
    for k in sorted(tool_args.keys()):
        v = tool_args.get(k)
        if isinstance(v, list):
            out[k] = sorted(v) if all(isinstance(x, str) for x in v) else v
        else:
            out[k] = v
    return out


def _tool_cache_key(tool_name: str, tool_args: Dict[str, Any]) -> str:
    normalized = _normalize_tool_args(tool_args)
    return f"{tool_name}:{json.dumps(normalized, ensure_ascii=False, sort_keys=True)}"


def _trim_text(s: Any, max_chars: int = 160) -> str:
    text = str(s or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _summarize_tool_result(tool_name: str, tool_result: Any) -> str:
    if not isinstance(tool_result, dict):
        return json.dumps({"tool": tool_name, "result": _trim_text(tool_result)}, ensure_ascii=False)
    summary: Dict[str, Any] = {
        "tool": tool_name,
        "success": bool(tool_result.get("success", False)),
        "via": tool_result.get("via", "unknown"),
    }
    if "error" in tool_result and tool_result.get("error"):
        summary["error"] = _trim_text(tool_result.get("error"), 200)
    if tool_name == "memory_search":
        results = tool_result.get("results")
        snippets: List[str] = []
        if isinstance(results, list):
            for r in results[:2]:
                if isinstance(r, dict):
                    txt = r.get("content") or r.get("summary") or r.get("text") or ""
                else:
                    txt = str(r)
                txt = _trim_text(txt, 120)
                if txt:
                    snippets.append(txt)
        summary["result_count"] = len(results) if isinstance(results, list) else 0
        if snippets:
            summary["snippets"] = snippets
    elif tool_name == "calculator":
        summary["value"] = tool_result.get("result", tool_result.get("value"))
    else:
        for key in ("result", "content", "message"):
            if key in tool_result and tool_result.get(key):
                summary[key] = _trim_text(tool_result.get(key), 160)
                break
    return json.dumps(summary, ensure_ascii=False)


def _stream_text_chunks(text: str, chunk_chars: int = 80):
    s = str(text or "")
    if not s:
        return
    step = max(1, int(chunk_chars))
    for i in range(0, len(s), step):
        yield s[i:i + step]


def _trace_tag() -> str:
    trace = get_active_trace()
    if trace is None:
        return ""
    request_id = str((trace.meta or {}).get("request_id", "")).strip()
    rid = request_id if request_id else "unknown"
    return f" request_id={rid} trace_id={trace.trace_id}"


class LLMClient:
    """OpenAI 兼容的 LLM 客户端封装。"""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.model = model or os.getenv("DEFAULT_MODEL", "gpt-3.5-turbo")
        self.provider = _provider_from_base_url(self.base_url)
        
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
        """发起普通对话请求并返回完整文本。"""
        prompt_tokens_est = estimate_prompt_tokens(messages)
        t0 = perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs
            )
            prompt_tokens, completion_tokens = _usage_tokens(response)
            add_event(
                "llm_call",
                model=self.model,
                provider=self.provider,
                llm_ms=(perf_counter() - t0) * 1000.0,
                first_token_latency_ms=None,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                prompt_tokens_est=prompt_tokens_est,
                success=True,
                stream=False,
            )
            return response.choices[0].message.content
        except Exception as e:
            add_event(
                "llm_call",
                model=self.model,
                provider=self.provider,
                llm_ms=(perf_counter() - t0) * 1000.0,
                first_token_latency_ms=None,
                prompt_tokens=None,
                completion_tokens=None,
                prompt_tokens_est=prompt_tokens_est,
                success=False,
                stream=False,
                error=str(e),
            )
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
        logger.info("[tools] call.start tools=%s%s", tool_names, _trace_tag())
        messages = list(messages)
        max_rounds = 6  # 最多 6 轮工具调用，防止死循环
        tool_cache: Dict[str, Dict[str, Any]] = {}
        last_exec_ms: Dict[str, float] = {}
        dedup_min_interval_ms = float(os.getenv("TOOL_DEDUP_MIN_INTERVAL_MS", "1500"))

        try:
            for round_idx in range(max_rounds):
                t_llm = perf_counter()
                prompt_tokens_est = estimate_prompt_tokens(messages)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                llm_ms = (perf_counter() - t_llm) * 1000.0
                prompt_tokens, completion_tokens = _usage_tokens(response)
                msg = response.choices[0].message
                has_tool_calls = bool(msg.tool_calls)
                add_event(
                    "llm_call",
                    model=self.model,
                    provider=self.provider,
                    llm_ms=llm_ms,
                    first_token_latency_ms=None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    prompt_tokens_est=prompt_tokens_est,
                    success=True,
                    stream=False,
                    with_tools=True,
                    round=round_idx + 1,
                    tool_round_count=round_idx + 1,
                    final_output_source=(
                        "assistant_last_round" if (not has_tool_calls and (msg.content or "").strip()) else None
                    ),
                    final_output_regen=False,
                )
                if not has_tool_calls:
                    logger.info(
                        "[tools] round=%d no_more_tool_calls llm_ms=%.1f%s",
                        round_idx + 1,
                        llm_ms,
                        _trace_tag(),
                    )
                    return msg.content or ""

                requested = [tc.function.name for tc in msg.tool_calls]
                logger.info(
                    "[tools] round=%d requested=%s llm_ms=%.1f%s",
                    round_idx + 1,
                    requested,
                    llm_ms,
                    _trace_tag(),
                )

                # 关键步骤：把模型的 tool_calls 意图写回消息历史，供下一轮参考。
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

                # 关键步骤：逐个执行工具，并把工具输出写入 tool 角色消息。
                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                    cache_key = _tool_cache_key(tool_name, tool_args)
                    now_ms = perf_counter() * 1000.0
                    dedup_reason = None
                    if cache_key in tool_cache:
                        dedup_reason = "exact_match_cache"
                    elif (
                        tool_name == "memory_search"
                        and cache_key in last_exec_ms
                        and (now_ms - float(last_exec_ms.get(cache_key, 0.0))) < dedup_min_interval_ms
                        and cache_key in tool_cache
                    ):
                        dedup_reason = "memory_search_min_interval"

                    t_tool = perf_counter()
                    if dedup_reason:
                        tool_result = dict(tool_cache.get(cache_key, {}))
                        add_event(
                            "tool_dedup",
                            tool_name=tool_name,
                            dedup_hit=True,
                            dedup_reason=dedup_reason,
                            tool_round=round_idx + 1,
                        )
                        logger.info("[tools] dedup_hit name=%s reason=%s%s", tool_name, dedup_reason, _trace_tag())
                    else:
                        logger.debug("[tools] execute name=%s args=%s", tool_name, tool_args)
                        tool_result = MCPTools.call_tool(tool_name, **tool_args)
                        tool_cache[cache_key] = dict(tool_result) if isinstance(tool_result, dict) else {"result": str(tool_result)}
                        last_exec_ms[cache_key] = perf_counter() * 1000.0
                        add_event(
                            "tool_dedup",
                            tool_name=tool_name,
                            dedup_hit=False,
                            dedup_reason="executed",
                            tool_round=round_idx + 1,
                        )
                        logger.info(
                            "[tools] executed name=%s success=%s via=%s elapsed_ms=%.1f%s",
                            tool_name,
                            bool(tool_result.get("success", False)) if isinstance(tool_result, dict) else False,
                            tool_result.get("via", "unknown") if isinstance(tool_result, dict) else "unknown",
                            (perf_counter() - t_tool) * 1000,
                            _trace_tag(),
                        )
                    logger.debug("[tools] result name=%s body=%s", tool_name, str(tool_result)[:300])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _summarize_tool_result(tool_name, tool_result),
                    })

            # 兜底策略：超过最大轮数后，不再继续调用工具，直接请求一次最终回答。
            logger.warning("[tools] max_rounds_reached=%d force_final_completion=1%s", max_rounds, _trace_tag())
            final = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            add_event(
                "llm_call",
                model=self.model,
                provider=self.provider,
                llm_ms=None,
                first_token_latency_ms=None,
                prompt_tokens=_usage_tokens(final)[0],
                completion_tokens=_usage_tokens(final)[1],
                prompt_tokens_est=estimate_prompt_tokens(messages),
                success=True,
                stream=False,
                with_tools=True,
                round="final_fallback",
            )
            return final.choices[0].message.content or ""

        except Exception as e:
            logger.exception("[tools] call.error fallback_to_plain_chat=1%s", _trace_tag())
            return self.chat(messages, temperature, max_tokens)

    def chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        **kwargs
    ):
        """发起流式对话请求，逐片段返回文本。"""
        prompt_tokens_est = estimate_prompt_tokens(messages)
        t0 = perf_counter()
        first_token_latency_ms: Optional[float] = None
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        try:
            # 尽量请求 provider 返回流式 usage，不支持时自动降级重试。
            request_kwargs = dict(kwargs)
            request_kwargs["stream_options"] = {"include_usage": True}
            try:
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    stream=True,
                    **request_kwargs
                )
            except Exception as ex:
                err = str(ex).lower()
                unsupported = (
                    "stream_options" in err
                    or "include_usage" in err
                    or "unknown argument" in err
                    or "extra_forbidden" in err
                    or "unexpected keyword" in err
                )
                if not unsupported:
                    raise
                stream = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    stream=True,
                    **kwargs
                )
            for chunk in stream:
                p, c = _usage_tokens(chunk)
                if p is not None:
                    prompt_tokens = p
                if c is not None:
                    completion_tokens = c
                choices = getattr(chunk, "choices", None)
                if isinstance(choices, list) and choices and choices[0].delta.content:
                    if first_token_latency_ms is None:
                        first_token_latency_ms = (perf_counter() - t0) * 1000.0
                    yield chunk.choices[0].delta.content
            add_event(
                "llm_call",
                model=self.model,
                provider=self.provider,
                llm_ms=(perf_counter() - t0) * 1000.0,
                first_token_latency_ms=first_token_latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                prompt_tokens_est=prompt_tokens_est,
                success=True,
                stream=True,
            )
        except Exception as e:
            add_event(
                "llm_call",
                model=self.model,
                provider=self.provider,
                llm_ms=(perf_counter() - t0) * 1000.0,
                first_token_latency_ms=first_token_latency_ms,
                prompt_tokens=None,
                completion_tokens=None,
                prompt_tokens_est=prompt_tokens_est,
                success=False,
                stream=True,
                error=str(e),
            )
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

        def _status_for_tool(tool_name: str) -> str:
            mapping = {
                "calculator": "正在使用计算器工具...",
                "websearch": "正在进行网络搜索...",
                "memory_search": "正在检索历史记忆...",
                "mindmap_generator": "正在生成思维导图结构...",
                "filewriter": "正在写入笔记文件...",
                "get_datetime": "正在查询当前日期时间...",
            }
            return mapping.get(tool_name, f"正在调用工具：{tool_name}...")

        if not tools:
            yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)
            return

        tool_names = [t["function"]["name"] for t in tools]
        logger.info("[stream_tools] call.start tools=%s%s", tool_names, _trace_tag())
        messages = list(messages)
        max_rounds = 6
        tool_cache: Dict[str, Dict[str, Any]] = {}
        last_exec_ms: Dict[str, float] = {}
        dedup_min_interval_ms = float(os.getenv("TOOL_DEDUP_MIN_INTERVAL_MS", "1500"))

        try:
            yield {"__status__": "模型正在分析问题..."}
            for round_idx in range(max_rounds):
                t_llm = perf_counter()
                prompt_tokens_est = estimate_prompt_tokens(messages)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                llm_ms = (perf_counter() - t_llm) * 1000.0
                prompt_tokens, completion_tokens = _usage_tokens(response)
                msg = response.choices[0].message
                has_tool_calls = bool(msg.tool_calls)
                final_text = (msg.content or "").strip()
                should_regen = (not has_tool_calls) and (not final_text)
                add_event(
                    "llm_call",
                    model=self.model,
                    provider=self.provider,
                    llm_ms=llm_ms,
                    first_token_latency_ms=None,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    prompt_tokens_est=prompt_tokens_est,
                    success=True,
                    stream=False,
                    with_tools=True,
                    round=round_idx + 1,
                    stream_tools=True,
                    tool_round_count=round_idx + 1,
                    final_output_source=(
                        "assistant_last_round"
                        if ((not has_tool_calls) and final_text)
                        else ("regen_fallback" if should_regen else None)
                    ),
                    final_output_regen=bool(should_regen),
                )

                if not has_tool_calls:
                    logger.info(
                        "[stream_tools] round=%d no_more_tool_calls llm_ms=%.1f final_source=%s%s",
                        round_idx + 1,
                        llm_ms,
                        "assistant_last_round" if final_text else "regen_fallback",
                        _trace_tag(),
                    )
                    if final_text:
                        yield {"__status__": "正在输出最终答案..."}
                        yield from _stream_text_chunks(final_text)
                        return
                    yield {"__status__": "正在补全最终答案..."}
                    yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)
                    return

                requested = [tc.function.name for tc in msg.tool_calls]
                logger.info(
                    "[stream_tools] round=%d requested=%s llm_ms=%.1f%s",
                    round_idx + 1,
                    requested,
                    llm_ms,
                    _trace_tag(),
                )
                for tool_name in dict.fromkeys(requested):
                    yield {"__status__": _status_for_tool(tool_name)}

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
                    cache_key = _tool_cache_key(tool_name, tool_args)
                    now_ms = perf_counter() * 1000.0
                    dedup_reason = None
                    if cache_key in tool_cache:
                        dedup_reason = "exact_match_cache"
                    elif (
                        tool_name == "memory_search"
                        and cache_key in last_exec_ms
                        and (now_ms - float(last_exec_ms.get(cache_key, 0.0))) < dedup_min_interval_ms
                        and cache_key in tool_cache
                    ):
                        dedup_reason = "memory_search_min_interval"

                    t_tool = perf_counter()
                    if dedup_reason:
                        tool_result = dict(tool_cache.get(cache_key, {}))
                        add_event(
                            "tool_dedup",
                            tool_name=tool_name,
                            dedup_hit=True,
                            dedup_reason=dedup_reason,
                            tool_round=round_idx + 1,
                        )
                        logger.info("[stream_tools] dedup_hit name=%s reason=%s%s", tool_name, dedup_reason, _trace_tag())
                    else:
                        logger.debug("[stream_tools] execute name=%s args=%s", tool_name, tool_args)
                        tool_result = MCPTools.call_tool(tool_name, **tool_args)
                        tool_cache[cache_key] = dict(tool_result) if isinstance(tool_result, dict) else {"result": str(tool_result)}
                        last_exec_ms[cache_key] = perf_counter() * 1000.0
                        add_event(
                            "tool_dedup",
                            tool_name=tool_name,
                            dedup_hit=False,
                            dedup_reason="executed",
                            tool_round=round_idx + 1,
                        )
                        logger.info(
                            "[stream_tools] executed name=%s success=%s via=%s elapsed_ms=%.1f%s",
                            tool_name,
                            bool(tool_result.get("success", False)) if isinstance(tool_result, dict) else False,
                            tool_result.get("via", "unknown") if isinstance(tool_result, dict) else "unknown",
                            (perf_counter() - t_tool) * 1000,
                            _trace_tag(),
                        )
                    logger.debug("[stream_tools] result name=%s body=%s", tool_name, str(tool_result)[:300])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _summarize_tool_result(tool_name, tool_result),
                    })
                yield {"__status__": "工具调用完成，继续推理中..."}
                add_event(
                    "tool_round_status",
                    tool_round=round_idx + 1,
                    status="tool_completed_continue_reasoning",
                )

            logger.warning("[stream_tools] max_rounds_reached=%d force_stream_final=1%s", max_rounds, _trace_tag())
            yield {"__status__": "工具调用轮次已达上限，正在整理答案..."}
            yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)

        except Exception as e:
            logger.exception("[stream_tools] call.error fallback_to_stream_plain=1%s", _trace_tag())
            yield {"__status__": "工具调用异常，正在降级生成回答..."}
            yield f"（工具调用出错，降级回答）\n"
            yield from self.chat_stream(messages, temperature, max_tokens=max_tokens)


# 全局 LLMClient 单例：避免每次请求重复创建 HTTP 客户端与连接池。
_llm_client = None


def get_llm_client() -> LLMClient:
    """获取全局 LLMClient 单例（不存在时自动创建）。"""
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
