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
import re
from typing import List, Dict, Any, Optional
from time import perf_counter
from openai import OpenAI
from dotenv import load_dotenv
from core.metrics import add_event, estimate_prompt_tokens, estimate_text_tokens, get_active_trace
from core.orchestration.policies import ToolPolicy, ToolCapability

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


def _tool_cache_key(tool_name: str, tool_args: Dict[str, Any]) -> str:
    return ToolPolicy.normalized_tool_signature(tool_name, tool_args)


def _trim_text(s: Any, max_chars: int = 160) -> str:
    text = str(s or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _trim_by_tokens(text: str, max_tokens: int) -> str:
    s = str(text or "").strip()
    if not s or max_tokens <= 0:
        return ""
    est = estimate_text_tokens(s)
    if est <= max_tokens:
        return s
    ratio = max_tokens / max(1, est)
    target_chars = max(80, int(len(s) * ratio))
    return _trim_text(s, target_chars)


def _extract_section(text: str, start_markers: List[str], stop_markers: List[str]) -> str:
    src = str(text or "")
    low = src.lower()
    start_idx = -1
    for marker in start_markers:
        i = low.find(marker.lower())
        if i >= 0 and (start_idx < 0 or i < start_idx):
            start_idx = i
    if start_idx < 0:
        return ""
    end_idx = len(src)
    for marker in stop_markers:
        j = low.find(marker.lower(), start_idx + 1)
        if j >= 0 and j < end_idx:
            end_idx = j
    return src[start_idx:end_idx].strip()


def _extract_user_goal(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    patterns = [
        r"用户问题[:：]\s*(.+)",
        r"用户当前消息[:：]\s*(.+)",
        r"用户请求[:：]\s*(.+)",
    ]
    for p in patterns:
        m = re.search(p, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            goal = m.group(1).strip().splitlines()[0].strip()
            if goal:
                return goal[:240]
    tail = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if tail:
        return tail[-1][:240]
    return text[:240]


def _build_compact_user_content(
    user_content: str,
    rag_max_tokens: int,
    memory_max_tokens: int,
) -> str:
    content = str(user_content or "").strip()
    if not content:
        return ""
    goal = _extract_user_goal(content)
    stop_markers = ["\n\n用户问题", "\n\n用户当前消息", "\n\n请按", "\n\n输出要求"]
    rag_raw = _extract_section(content, ["【教材参考】", "教材参考资料"], stop_markers)
    mem_raw = _extract_section(content, ["【该知识点历史错题参考", "【历史错题", "【用户学习档案】"], stop_markers)

    rag_summary = _trim_by_tokens(rag_raw, rag_max_tokens) if rag_raw else ""
    mem_summary = _trim_by_tokens(mem_raw, memory_max_tokens) if mem_raw else ""
    out = [f"【任务目标】{goal or _trim_text(content, 220)}"]
    if rag_summary:
        out.append("【必要证据摘要】\n" + rag_summary)
    if mem_summary:
        out.append("【近期记忆摘要】\n" + mem_summary)
    return "\n\n".join(out).strip()


def _is_request_cache_enabled() -> bool:
    if not _env_bool("MEMORY_DEDUP_ENABLE", True):
        return False
    scope = str(os.getenv("MEMORY_DEDUP_SCOPE", "request")).strip().lower()
    return scope == "request"


def _request_cache_get(cache_key: str) -> Optional[Dict[str, Any]]:
    if not _is_request_cache_enabled():
        return None
    trace = get_active_trace()
    if trace is None:
        return None
    cache = (trace.meta or {}).get("_request_memory_cache")
    if not isinstance(cache, dict):
        return None
    v = cache.get(cache_key)
    return dict(v) if isinstance(v, dict) else None


def _request_cache_put(cache_key: str, value: Dict[str, Any]) -> None:
    if not _is_request_cache_enabled():
        return
    trace = get_active_trace()
    if trace is None or not isinstance(value, dict):
        return
    if not isinstance(trace.meta, dict):
        trace.meta = {}
    cache = trace.meta.get("_request_memory_cache")
    if not isinstance(cache, dict):
        cache = {}
        trace.meta["_request_memory_cache"] = cache
    max_entries = max(1, _env_int("MEMORY_DEDUP_MAX_ENTRIES", 64))
    if len(cache) >= max_entries and cache_key not in cache:
        oldest = next(iter(cache.keys()))
        cache.pop(oldest, None)
    cache[cache_key] = dict(value)


def _anchor_system_and_user(messages: List[Dict[str, Any]]) -> tuple[Optional[Dict[str, Any]], Optional[int]]:
    system_msg = None
    user_idx = None
    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "system" and system_msg is None:
            system_msg = dict(m)
        if role == "user":
            user_idx = idx
            break
    return system_msg, user_idx


def _compact_messages_for_tool_round(
    messages: List[Dict[str, Any]],
    original_user_content: str,
    round_no: int,
) -> List[Dict[str, Any]]:
    full_rounds = max(1, _env_int("TOOL_ROUND_FULL_CONTEXT_ROUNDS", 1))
    if round_no <= full_rounds:
        return messages
    keep_last_tool_msgs = max(1, _env_int("TOOL_ROUND_KEEP_LAST_TOOL_MSGS", 2))
    rag_max_tokens = max(80, _env_int("TOOL_ROUND_RAG_SUMMARY_MAX_TOKENS", 400))
    memory_max_tokens = max(40, _env_int("TOOL_ROUND_MEMORY_SUMMARY_MAX_TOKENS", 180))

    system_msg, user_idx = _anchor_system_and_user(messages)
    if user_idx is None:
        return messages

    compact_user = _build_compact_user_content(
        original_user_content,
        rag_max_tokens=rag_max_tokens,
        memory_max_tokens=memory_max_tokens,
    )
    out: List[Dict[str, Any]] = []
    if system_msg is not None:
        out.append(system_msg)
    out.append({"role": "user", "content": compact_user or _trim_text(original_user_content, 320)})

    middle = messages[user_idx + 1 :]
    # 保持 assistant(tool_calls) 与 tool 响应配对，避免出现孤立 tool 消息导致 provider 400。
    segments: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    for m in middle:
        role = m.get("role")
        if role == "assistant":
            if current:
                segments.append(current)
            current = [dict(m)]
            continue
        if role == "tool":
            if not current:
                # 丢弃无前导 assistant 的孤立 tool 消息
                continue
            current.append(dict(m))
            continue
        if current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)

    keep_segments = max(1, keep_last_tool_msgs)
    for seg in segments[-keep_segments:]:
        out.extend(seg)
    return out


def _rehydrate_messages_for_final(
    messages: List[Dict[str, Any]],
    original_user_content: str,
) -> List[Dict[str, Any]]:
    if not _env_bool("TOOL_FINAL_REHYDRATE", True):
        return messages
    mode = str(os.getenv("TOOL_FINAL_REHYDRATE_MODE", "summary")).strip().lower()
    out = [dict(m) for m in messages]
    _, user_idx = _anchor_system_and_user(out)
    if user_idx is None:
        return out
    if mode == "full":
        out[user_idx]["content"] = original_user_content
        return out
    # summary mode
    out[user_idx]["content"] = _build_compact_user_content(
        original_user_content,
        rag_max_tokens=max(120, _env_int("TOOL_ROUND_RAG_SUMMARY_MAX_TOKENS", 400) * 2),
        memory_max_tokens=max(80, _env_int("TOOL_ROUND_MEMORY_SUMMARY_MAX_TOKENS", 180) * 2),
    )
    return out


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
    from time import sleep

    s = str(text or "")
    if not s:
        return
    env_chunk = _env_int("TOOL_STREAM_TEXT_CHUNK_CHARS", chunk_chars)
    env_delay_ms = max(0.0, _env_float("TOOL_STREAM_TEXT_CHUNK_DELAY_MS", 12.0))
    step = max(1, int(env_chunk))
    for i in range(0, len(s), step):
        yield s[i:i + step]
        if env_delay_ms > 0:
            sleep(env_delay_ms / 1000.0)


def _trace_tag() -> str:
    trace = get_active_trace()
    if trace is None:
        return ""
    request_id = str((trace.meta or {}).get("request_id", "")).strip()
    rid = request_id if request_id else "unknown"
    return f" request_id={rid} trace_id={trace.trace_id}"


def _active_mode() -> str:
    trace = get_active_trace()
    if trace is None or not isinstance(trace.meta, dict):
        return "learn"
    mode = str(trace.meta.get("mode", "learn")).strip().lower()
    return mode if mode in {"learn", "practice", "exam"} else "learn"


def _tool_failure_class(tool_result: Dict[str, Any]) -> str:
    if not isinstance(tool_result, dict):
        return "fatal_error"
    if bool(tool_result.get("success", False)):
        return "success"
    err = str(tool_result.get("error", "")).lower()
    retryable_signals = (
        "timeout",
        "temporarily",
        "connection",
        "refused",
        "reset",
        "429",
        "rate limit",
        "unavailable",
        "network",
    )
    if any(sig in err for sig in retryable_signals):
        return "retryable_error"
    return "fatal_error"


def _memory_search_intent_from_user_goal(user_content: str) -> bool:
    text = str(user_content or "").lower()
    if not text:
        return False
    signals = ("之前", "历史", "错题", "记忆", "复习", "上次", "薄弱点", "以前", "past", "history")
    return any(sig in text for sig in signals)


def _tool_call_preflight(
    *,
    tool_name: str,
    tool_args: Dict[str, Any],
    phase: str,
    original_user_content: str,
) -> tuple[bool, str, ToolCapability, str]:
    allow_memory_in_act = _env_bool("MEMORY_SEARCH_IN_ACT_DEFAULT", False)
    if tool_name == "memory_search" and not allow_memory_in_act:
        allow_memory_in_act = _memory_search_intent_from_user_goal(original_user_content)
    return ToolPolicy.tool_preflight(
        tool_name=tool_name,
        tool_args=tool_args,
        mode=_active_mode(),
        phase=phase,
        memory_search_in_act_default=allow_memory_in_act,
    )


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
        """带 Function Calling 的对话（显式 Act/Synthesize 两阶段，非流式最终输出）。"""
        logger = logging.getLogger("llm.tools")
        from mcp_tools.client import MCPTools

        if not tools:
            return self.chat(messages, temperature, max_tokens)

        tool_names = [t["function"]["name"] for t in tools]
        logger.info("[tools] call.start tools=%s%s", tool_names, _trace_tag())
        messages = list(messages)
        _, _user_idx = _anchor_system_and_user(messages)
        original_user_content = str(messages[_user_idx].get("content", "")) if _user_idx is not None else ""
        max_rounds = max(1, _env_int("ACT_MAX_ROUNDS", 4))
        act_max_tokens = max(64, _env_int("ACT_MAX_TOKENS", 160))
        if isinstance(max_tokens, int) and max_tokens > 0:
            act_max_tokens = min(act_max_tokens, max_tokens)
        tool_retry_max = max(0, _env_int("TOOL_RETRY_MAX", 1))
        tool_cache: Dict[str, Dict[str, Any]] = {}
        last_exec_ms: Dict[str, float] = {}
        dedup_min_interval_ms = _env_float("TOOL_DEDUP_MIN_INTERVAL_MS", 1500.0)
        fallback_triggered = False
        act_last_content = ""

        try:
            add_event("react_phase", phase="act", round=1)
            for round_idx in range(max_rounds):
                round_no = round_idx + 1
                messages = _compact_messages_for_tool_round(
                    messages=messages,
                    original_user_content=original_user_content,
                    round_no=round_no,
                )
                t_llm = perf_counter()
                prompt_tokens_est = estimate_prompt_tokens(messages)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=act_max_tokens,
                )
                llm_ms = (perf_counter() - t_llm) * 1000.0
                prompt_tokens, completion_tokens = _usage_tokens(response)
                msg = response.choices[0].message
                has_tool_calls = bool(msg.tool_calls)
                final_text = (msg.content or "").strip()
                act_last_content = final_text or act_last_content
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
                    round=round_no,
                    tool_round_count=round_no,
                    react_phase="act",
                    final_output_source=("act_only_no_tool_calls" if (not has_tool_calls and final_text) else None),
                    final_output_regen=False,
                )
                if not has_tool_calls:
                    logger.info(
                        "[tools] round=%d no_more_tool_calls llm_ms=%.1f -> synthesize%s",
                        round_no,
                        llm_ms,
                        _trace_tag(),
                    )
                    break

                requested = [tc.function.name for tc in msg.tool_calls]
                logger.info(
                    "[tools] round=%d requested=%s llm_ms=%.1f%s",
                    round_no,
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

                    allowed, gate_reason, capability, cache_key = _tool_call_preflight(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        phase="act",
                        original_user_content=original_user_content,
                    )
                    add_event(
                        "tool_gate_decision",
                        tool_name=tool_name,
                        phase="act",
                        tool_gate_decision=allowed,
                        tool_skip_reason=None if allowed else gate_reason,
                        tool_signature=cache_key,
                        tool_round=round_no,
                    )
                    if not allowed:
                        add_event(
                            "tool_skip",
                            tool_name=tool_name,
                            tool_skip_reason=gate_reason,
                            tool_signature=cache_key,
                            tool_round=round_no,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(
                                {
                                    "tool": tool_name,
                                    "success": False,
                                    "error": f"tool gated: {gate_reason}",
                                    "failure_class": "fatal_error",
                                },
                                ensure_ascii=False,
                            ),
                        })
                        continue

                    now_ms = perf_counter() * 1000.0
                    dedup_reason = None
                    if cache_key in tool_cache:
                        dedup_reason = "exact_match_cache"
                    elif tool_name == "memory_search":
                        shared = _request_cache_get(cache_key)
                        if isinstance(shared, dict):
                            tool_cache[cache_key] = dict(shared)
                            dedup_reason = "request_scope_cache"
                        elif (
                            cache_key in last_exec_ms
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
                            tool_round=round_no,
                        )
                        logger.info("[tools] dedup_hit name=%s reason=%s%s", tool_name, dedup_reason, _trace_tag())
                    else:
                        logger.debug("[tools] execute name=%s args=%s", tool_name, tool_args)
                        attempts = 0
                        max_attempts = max(1, 1 + min(tool_retry_max, 1 if capability.retry_policy == "once" else 0))
                        tool_result: Dict[str, Any] = {}
                        failure_class = "fatal_error"
                        while attempts < max_attempts:
                            attempts += 1
                            tool_result = MCPTools.call_tool(tool_name, **tool_args)
                            failure_class = _tool_failure_class(tool_result if isinstance(tool_result, dict) else {})
                            if failure_class != "retryable_error" or attempts >= max_attempts:
                                break
                            add_event(
                                "tool_retry_count",
                                tool_name=tool_name,
                                tool_retry_count=attempts,
                                tool_failure_class=failure_class,
                                tool_round=round_no,
                            )
                        tool_result = dict(tool_result) if isinstance(tool_result, dict) else {"result": str(tool_result)}
                        tool_result.setdefault("failure_class", failure_class)
                        tool_cache[cache_key] = dict(tool_result)
                        if tool_name == "memory_search" and isinstance(tool_result, dict):
                            _request_cache_put(cache_key, tool_result)
                        last_exec_ms[cache_key] = perf_counter() * 1000.0
                        add_event(
                            "tool_dedup",
                            tool_name=tool_name,
                            dedup_hit=False,
                            dedup_reason="executed",
                            tool_round=round_no,
                        )
                        add_event(
                            "tool_failure_class",
                            tool_name=tool_name,
                            tool_failure_class=failure_class,
                            tool_retry_count=max(0, attempts - 1),
                            tool_round=round_no,
                        )
                        logger.info(
                            "[tools] executed name=%s success=%s via=%s elapsed_ms=%.1f%s",
                            tool_name,
                            bool(tool_result.get("success", False)) if isinstance(tool_result, dict) else False,
                            tool_result.get("via", "unknown") if isinstance(tool_result, dict) else "unknown",
                            (perf_counter() - t_tool) * 1000,
                            _trace_tag(),
                        )
                        if failure_class in {"retryable_error", "fatal_error"} and capability.fallback_mode == "synthesize":
                            fallback_triggered = True
                            add_event(
                                "tool_fallback_triggered",
                                tool_name=tool_name,
                                tool_failure_class=failure_class,
                                tool_round=round_no,
                                tool_fallback_triggered=True,
                            )
                    logger.debug("[tools] result name=%s body=%s", tool_name, str(tool_result)[:300])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _summarize_tool_result(tool_name, tool_result),
                    })
                    if fallback_triggered:
                        break
                if fallback_triggered:
                    logger.info("[tools] act_stop_on_tool_failure round=%d%s", round_no, _trace_tag())
                    break

            add_event("react_phase", phase="synthesize", round="final")
            logger.info("[tools] phase=synthesize fallback=%s%s", int(fallback_triggered), _trace_tag())
            final_messages = _rehydrate_messages_for_final(messages, original_user_content)
            if act_last_content:
                final_messages = list(final_messages) + [
                    {"role": "assistant", "content": _trim_text(act_last_content, 220)},
                    {"role": "user", "content": "请基于以上工具结果，给出最终完整回答。"},
                ]
            t_synth = perf_counter()
            final = self.client.chat.completions.create(
                model=self.model,
                messages=final_messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            synth_prompt_tokens, synth_completion_tokens = _usage_tokens(final)
            add_event(
                "llm_call",
                model=self.model,
                provider=self.provider,
                llm_ms=(perf_counter() - t_synth) * 1000.0,
                first_token_latency_ms=None,
                prompt_tokens=synth_prompt_tokens,
                completion_tokens=synth_completion_tokens,
                prompt_tokens_est=estimate_prompt_tokens(final_messages),
                success=True,
                stream=False,
                with_tools=True,
                round="synthesize_final",
                react_phase="synthesize",
                final_output_source="synthesize_round",
                final_output_regen=False,
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
        """工具调用（Act 非流式）+ 最终答案（Synthesize 流式）。"""
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
        _, _user_idx = _anchor_system_and_user(messages)
        original_user_content = str(messages[_user_idx].get("content", "")) if _user_idx is not None else ""
        max_rounds = max(1, _env_int("ACT_MAX_ROUNDS", 4))
        act_max_tokens = max(64, _env_int("ACT_MAX_TOKENS", 160))
        if isinstance(max_tokens, int) and max_tokens > 0:
            act_max_tokens = min(act_max_tokens, max_tokens)
        tool_retry_max = max(0, _env_int("TOOL_RETRY_MAX", 1))
        tool_cache: Dict[str, Dict[str, Any]] = {}
        last_exec_ms: Dict[str, float] = {}
        dedup_min_interval_ms = _env_float("TOOL_DEDUP_MIN_INTERVAL_MS", 1500.0)
        fallback_triggered = False
        act_last_content = ""

        try:
            add_event("react_phase", phase="act", round=1, stream_tools=True)
            yield {"__status__": "模型正在分析问题（Plan）..."}
            for round_idx in range(max_rounds):
                round_no = round_idx + 1
                messages = _compact_messages_for_tool_round(
                    messages=messages,
                    original_user_content=original_user_content,
                    round_no=round_no,
                )
                t_llm = perf_counter()
                prompt_tokens_est = estimate_prompt_tokens(messages)
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=temperature,
                    max_tokens=act_max_tokens,
                )
                llm_ms = (perf_counter() - t_llm) * 1000.0
                prompt_tokens, completion_tokens = _usage_tokens(response)
                msg = response.choices[0].message
                has_tool_calls = bool(msg.tool_calls)
                final_text = (msg.content or "").strip()
                act_last_content = final_text or act_last_content
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
                    round=round_no,
                    stream_tools=True,
                    react_phase="act",
                    tool_round_count=round_no,
                    final_output_source=("act_only_no_tool_calls" if (not has_tool_calls) else None),
                    final_output_regen=False,
                )

                if not has_tool_calls:
                    logger.info(
                        "[stream_tools] round=%d no_more_tool_calls llm_ms=%.1f -> synthesize%s",
                        round_no,
                        llm_ms,
                        _trace_tag(),
                    )
                    if final_text and not _env_bool("ALWAYS_FINAL_STREAM", True):
                        yield {"__status__": "正在输出最终答案..."}
                        yield from _stream_text_chunks(final_text)
                        return
                    break

                requested = [tc.function.name for tc in msg.tool_calls]
                logger.info(
                    "[stream_tools] round=%d requested=%s llm_ms=%.1f%s",
                    round_no,
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

                    allowed, gate_reason, capability, cache_key = _tool_call_preflight(
                        tool_name=tool_name,
                        tool_args=tool_args,
                        phase="act",
                        original_user_content=original_user_content,
                    )
                    add_event(
                        "tool_gate_decision",
                        tool_name=tool_name,
                        phase="act",
                        tool_gate_decision=allowed,
                        tool_skip_reason=None if allowed else gate_reason,
                        tool_signature=cache_key,
                        tool_round=round_no,
                    )
                    if not allowed:
                        add_event(
                            "tool_skip",
                            tool_name=tool_name,
                            tool_skip_reason=gate_reason,
                            tool_signature=cache_key,
                            tool_round=round_no,
                        )
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(
                                {
                                    "tool": tool_name,
                                    "success": False,
                                    "error": f"tool gated: {gate_reason}",
                                    "failure_class": "fatal_error",
                                },
                                ensure_ascii=False,
                            ),
                        })
                        continue

                    now_ms = perf_counter() * 1000.0
                    dedup_reason = None
                    if cache_key in tool_cache:
                        dedup_reason = "exact_match_cache"
                    elif tool_name == "memory_search":
                        shared = _request_cache_get(cache_key)
                        if isinstance(shared, dict):
                            tool_cache[cache_key] = dict(shared)
                            dedup_reason = "request_scope_cache"
                        elif (
                            cache_key in last_exec_ms
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
                            tool_round=round_no,
                        )
                        logger.info("[stream_tools] dedup_hit name=%s reason=%s%s", tool_name, dedup_reason, _trace_tag())
                    else:
                        logger.debug("[stream_tools] execute name=%s args=%s", tool_name, tool_args)
                        attempts = 0
                        max_attempts = max(1, 1 + min(tool_retry_max, 1 if capability.retry_policy == "once" else 0))
                        tool_result: Dict[str, Any] = {}
                        failure_class = "fatal_error"
                        while attempts < max_attempts:
                            attempts += 1
                            tool_result = MCPTools.call_tool(tool_name, **tool_args)
                            failure_class = _tool_failure_class(tool_result if isinstance(tool_result, dict) else {})
                            if failure_class != "retryable_error" or attempts >= max_attempts:
                                break
                            add_event(
                                "tool_retry_count",
                                tool_name=tool_name,
                                tool_retry_count=attempts,
                                tool_failure_class=failure_class,
                                tool_round=round_no,
                            )
                        tool_result = dict(tool_result) if isinstance(tool_result, dict) else {"result": str(tool_result)}
                        tool_result.setdefault("failure_class", failure_class)
                        tool_cache[cache_key] = dict(tool_result)
                        if tool_name == "memory_search" and isinstance(tool_result, dict):
                            _request_cache_put(cache_key, tool_result)
                        last_exec_ms[cache_key] = perf_counter() * 1000.0
                        add_event(
                            "tool_dedup",
                            tool_name=tool_name,
                            dedup_hit=False,
                            dedup_reason="executed",
                            tool_round=round_no,
                        )
                        add_event(
                            "tool_failure_class",
                            tool_name=tool_name,
                            tool_failure_class=failure_class,
                            tool_retry_count=max(0, attempts - 1),
                            tool_round=round_no,
                        )
                        logger.info(
                            "[stream_tools] executed name=%s success=%s via=%s elapsed_ms=%.1f%s",
                            tool_name,
                            bool(tool_result.get("success", False)) if isinstance(tool_result, dict) else False,
                            tool_result.get("via", "unknown") if isinstance(tool_result, dict) else "unknown",
                            (perf_counter() - t_tool) * 1000,
                            _trace_tag(),
                        )
                        if failure_class in {"retryable_error", "fatal_error"} and capability.fallback_mode == "synthesize":
                            fallback_triggered = True
                            add_event(
                                "tool_fallback_triggered",
                                tool_name=tool_name,
                                tool_failure_class=failure_class,
                                tool_round=round_no,
                                tool_fallback_triggered=True,
                            )
                    logger.debug("[stream_tools] result name=%s body=%s", tool_name, str(tool_result)[:300])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _summarize_tool_result(tool_name, tool_result),
                    })
                    if fallback_triggered:
                        break
                if fallback_triggered:
                    logger.info("[stream_tools] act_stop_on_tool_failure round=%d%s", round_no, _trace_tag())
                    break
                yield {"__status__": "工具调用完成，继续推理中..."}
                add_event(
                    "tool_round_status",
                    tool_round=round_no,
                    status="tool_completed_continue_reasoning",
                )

            add_event("react_phase", phase="synthesize", round="final", stream_tools=True)
            logger.info("[stream_tools] phase=synthesize fallback=%s%s", int(fallback_triggered), _trace_tag())
            yield {"__status__": "工具调用完成，正在生成最终答案（Synthesize）..."}
            final_messages = _rehydrate_messages_for_final(messages, original_user_content)
            if act_last_content:
                final_messages = list(final_messages) + [
                    {
                        "role": "assistant",
                        "content": _trim_text(act_last_content, 220),
                    },
                    {
                        "role": "user",
                        "content": "请基于以上工具结果，直接给出对用户可见的最终完整回答。",
                    },
                ]
            yield from self.chat_stream(final_messages, temperature, max_tokens=max_tokens)

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
