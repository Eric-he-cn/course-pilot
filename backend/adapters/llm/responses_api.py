from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence

import httpx

from contracts.llm import (
    ChatDelta,
    ChatFinal,
    ChatMessage,
    ChatReasoning,
    ChatToolCalls,
    LLMProviderError,
    ServerToolCall,
    ToolCallRequest,
    ToolSpec,
)

from .http_chat import HttpChatBase

# 收尾事件；Responses 没有 data: [DONE]，流靠这三个之一结束。
_TERMINAL = frozenset({"response.completed", "response.incomplete", "response.failed"})
# 厂商端联网搜索：工具声明、它产出的条目类型，以及报给上层的那个名字。
_SERVER_SEARCH_KIND = "web_search"
_SERVER_SEARCH_TOOL: dict[str, object] = {"type": _SERVER_SEARCH_KIND}
_SERVER_SEARCH_ITEM = "web_search_call"
# 思考内容的两路事件。厂商实际用的是哪一路随增量带出去，开发者模式要显示它。
_REASONING = {"response.reasoning_text.delta": "reasoning_text",
              "response.reasoning_summary_text.delta": "reasoning_summary_text"}
# 截断原因 → Chat Completions 那套 finish_reason 的说法，界面与统计不必分协议。
_FINISH_REASONS = {"max_output_tokens": "length", "content_filter": "content_filter"}


def to_tools(tools: Sequence[ToolSpec]) -> list[dict[str, object]]:
    """Responses 的工具定义是平铺的，没有 Chat Completions 那层 function 嵌套。"""
    return [{"type": "function", "name": tool.name, "description": tool.description,
             "parameters": tool.parameters} for tool in tools]


def to_input(messages: Sequence[ChatMessage], *, echo_reasoning: bool = False) -> list[dict[str, object]]:
    """ChatMessage 列表 → Responses 的 input 条目。

    系统消息保留成 message 条目而不搬进 instructions：多条系统消息和它们的先后位置都不变。
    assistant 的工具调用拆成独立的 function_call 条目，工具结果是 function_call_output。
    厂商端跑过的调用原样发回去，服务端据此恢复它那边的搜索结果。
    """
    items: list[dict[str, object]] = []
    for message in messages:
        if message.role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": message.tool_call_id or "", "output": message.content})
        elif message.role == "assistant" and message.tool_calls:
            # 思考内容是独立条目，摆在它引出的那几个调用之前。
            if message.reasoning or echo_reasoning:
                items.append({"type": "reasoning",
                              "content": [{"type": "reasoning_text", "text": message.reasoning}]})
            # 厂商端的调用排在这一轮的思考之后、正文之前。真机实测：摆到思考前面，服务端会当成
            # 这一轮没回传思考内容而拒收；摆到 function_call 与它的结果之间则配不上对。
            items += [dict(call.echo) for call in message.server_calls if call.echo]
            if message.content:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": message.content}]})
            items += [{"type": "function_call", "call_id": call.id, "name": call.name,
                       "arguments": call.arguments} for call in message.tool_calls]
        elif message.role == "assistant":
            items += [dict(call.echo) for call in message.server_calls if call.echo]
            if message.content:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": message.content}]})
        else:
            items.append({"type": "message", "role": message.role, "content": message.content})
    return items


def _search_detail(action: dict) -> str:
    """一次厂商端调用做了什么，压成一行给界面看。

    查询词里最后一条是厂商塞进去的追踪串（ws_call_id=…），网址上也挂着同一个片段，
    展示时都摘掉；回传给厂商的那份保持原样。
    """
    queries = [str(item) for item in action.get("queries") or [] if not str(item).startswith("ws_call_id=")]
    detail = "、".join(queries) if queries else str(action.get("url") or "").split("#", 1)[0]
    return " ".join(detail.split())[:200]


def _server_calls(searches: dict[str, dict[str, object]]) -> tuple[ServerToolCall, ...]:
    """按开始顺序汇总这一轮厂商端跑过的调用。只有条目事件到齐的才算数——
    光有状态事件说明流被截断，那种半截记录说不出它做了什么。"""
    result: list[ServerToolCall] = []
    for call_id, entry in searches.items():
        item = entry.get("item")
        if not isinstance(item, dict):
            continue
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        result.append(ServerToolCall(
            id=call_id, kind=_SERVER_SEARCH_KIND,
            action=str(action.get("type") or ""), detail=_search_detail(action),
            ok=str(item.get("status") or "completed") != "failed",
            duration_ms=int(entry.get("duration_ms") or 0), echo=item,
        ))
    return tuple(result)


class ResponsesApiChat(HttpChatBase):
    """OpenAI Responses 协议适配器：语义与 Chat Completions 那条等价，只是换了一套线上格式。

    只用标准字段，任何实现该协议的服务都能接。厂商私有参数走 extra_body 传入。
    """

    endpoint_path = "/responses"
    protocol_fields = {
        **{name: "它是协议字段，覆盖了流式解析会崩在运行时"
           for name in ("model", "input", "stream", "tools", "max_output_tokens")},
        "instructions": "服务端会把它静默插成第一条 system 消息，顶在每一次请求（含学科分类器）的规则前面",
    }
    default_provider = "openai_responses"
    # 撞过一次「必须回传思考内容」之后，这个实例就一直带上 reasoning 条目。
    _echo_reasoning = False

    def __init__(self, *, server_search: bool = False, **options: object) -> None:
        super().__init__(**options)  # type: ignore[arg-type]
        # 厂商端联网搜索。默认关：它的结果不经过本地的不可信内容前缀，也产不出可点开的引用。
        self._server_search = server_search

    def chat(self, *, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> Iterator[ChatDelta | ChatReasoning | ChatToolCalls | ChatFinal]:
        payload: dict[str, object] = {
            "model": self._model,
            "input": to_input(messages, echo_reasoning=self._echo_reasoning),
            "max_output_tokens": self._max_output_tokens,
            "stream": True,
            # 无状态调用，不在厂商侧留会话记录。并行工具调用两边默认都开，不必显式声明。
            "store": False,
            **self._extra_body,
        }
        # extra_body 里置 null 表示移除该字段，留给不认识某个字段的服务一个出口。
        payload = {key: value for key, value in payload.items() if value is not None}
        wire_tools = [*to_tools(tools), *([_SERVER_SEARCH_TOOL] if self._server_search else [])]
        if wire_tools:
            payload["tools"] = wire_tools
        attempt = 0
        while True:
            emitted = False
            try:
                with self._client.stream("POST", self._endpoint, json=payload, headers=self._headers) as response:
                    # 出错时必须在这里读完 body：离开 with 之后流已关闭，服务端的说明就再也拿不到了。
                    if response.status_code >= 400:
                        response.read()
                    response.raise_for_status()
                    parts: list[str] = []
                    reasoning_parts: list[str] = []
                    calls: dict[object, dict[str, str]] = {}
                    # 同一次调用在不同事件上换着报 output_index / item_id，别名表把它们归到一个槽。
                    aliases: dict[object, object] = {}
                    # 厂商端的调用按它自己那个 id 归位，多次搜索可以并行、事件会交错。
                    searches: dict[str, dict[str, object]] = {}
                    usage: dict[str, int] = {}
                    status, reason, failure, event_name = "", "", None, ""
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event_name = line[len("event:"):].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data or data == "[DONE]":
                            continue
                        chunk = json.loads(data)
                        # 事件类型在负载里也有一份；只发 SSE event 行的服务靠后者兜底。
                        kind, event_name = str(chunk.get("type") or event_name), ""
                        if kind == "response.output_text.delta":
                            piece = chunk.get("delta")
                            if isinstance(piece, str) and piece:
                                emitted = True
                                parts.append(piece)
                                yield ChatDelta(piece)
                        elif kind in _REASONING:
                            # 思考内容单独走一路：它不进答案，但要回传给厂商。
                            # 刻意不设 emitted——答案还没开始，网络抖动时整轮重试仍然安全。
                            thinking = chunk.get("delta")
                            if isinstance(thinking, str) and thinking:
                                reasoning_parts.append(thinking)
                                yield ChatReasoning(thinking, field=_REASONING[kind])
                        elif kind == "response.function_call_arguments.delta":
                            fragment = chunk.get("delta")
                            if isinstance(fragment, str) and fragment:
                                self._slot(calls, aliases, chunk)["arguments"] += fragment
                        elif kind == "response.function_call_arguments.done":
                            arguments = chunk.get("arguments")
                            if isinstance(arguments, str) and arguments:
                                self._slot(calls, aliases, chunk)["arguments"] = arguments
                        elif kind in ("response.output_item.added", "response.output_item.done"):
                            item = chunk.get("item")
                            if isinstance(item, dict) and item.get("type") == _SERVER_SEARCH_ITEM:
                                self._absorb_search(searches, item, final=kind.endswith(".done"))
                            else:
                                self._absorb_item(calls, aliases, chunk, final=kind.endswith(".done"))
                        elif kind.startswith("response.web_search_call."):
                            # 状态事件只带 id：它是唯一能实时看到「正在搜」的信号，
                            # 做什么、成没成要等条目事件。收尾事件在失败的调用上照发。
                            self._mark_search(searches, str(chunk.get("item_id") or ""),
                                              done=kind.endswith(".completed"))
                        elif kind in _TERMINAL:
                            body = chunk.get("response") or {}
                            usage.update(self._usage(body.get("usage")))
                            status = str(body.get("status") or kind[len("response."):])
                            reason = str((body.get("incomplete_details") or {}).get("reason") or "")
                            # 先来的 error 事件说的才是原因，别被收尾那份空的 error 抹掉。
                            failure = failure or body.get("error")
                            # 这一轮失败了就不补正文：半截答案不该当成结果下发。
                            for piece in ([] if failure else self._recover(body.get("output"), parts, calls, searches)):
                                emitted = True
                                parts.append(piece)
                                yield ChatDelta(piece)
                            # 收尾事件之后不再读：服务端不关流的话读超时会把收全的答案整个丢掉。
                            break
                        elif kind == "error":
                            failure = chunk
                    if failure:
                        # 厂商说这次生成失败了。是不是能重试它没说，按不可重试处理，把原话带上。
                        self._record_failure("provider_failed")
                        detail = self._sanitize(failure.get("message") if isinstance(failure, dict) else None)
                        raise LLMProviderError("provider_failed", f"{self._provider} 生成失败{detail}", retryable=False)
                    if calls:
                        # 有 output_index 就按它排（模型给的先后），没有就保持到达顺序。
                        ordered = sorted(calls.items()) if all(isinstance(key, int) for key in calls) else list(calls.items())
                        requests = tuple(
                            ToolCallRequest(id=entry["id"] or f"call_{index}", name=entry["name"], arguments=entry["arguments"])
                            for index, (_, entry) in enumerate(ordered)
                        )
                        self._record_success()
                        yield ChatToolCalls(calls=requests, usage=usage, reasoning="".join(reasoning_parts),
                                            provider_finish_reason=reason or status or None,
                                            server_calls=_server_calls(searches))
                        return
                    text = "".join(parts).strip()
                    if not text:
                        # 空回答按收尾原因归类：推理模型思考吃完输出预算是常见态，
                        # 一律报「空回答」会让人查错方向。
                        code, why = {
                            "max_output_tokens": ("output_truncated", "输出在生成完正文前达到 token 上限"),
                            "content_filter": ("content_filtered", "输出被服务端内容过滤拦下"),
                        }.get(reason, ("invalid_response", "返回了空回答"))
                        self._record_failure(code)
                        raise LLMProviderError(code, f"{self._provider} {why}", retryable=False)
                    self._record_success()
                    yield ChatFinal(text=text, finish_reason=_FINISH_REASONS.get(reason, "stop"),
                                    provider=self.provider, model=self.model, mode=self.mode, usage=usage,
                                    provider_finish_reason=reason or status or None,
                                    server_calls=_server_calls(searches))
                    return
            except httpx.HTTPStatusError as error:
                # raise_for_status 在拿到任何 delta 之前触发，可以安全重试。
                status_code = error.response.status_code
                retryable = status_code == 429 or status_code >= 500
                if retryable and attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                detail = self._detail(error.response)
                # 思考模式要求把上一轮的思考内容回传，撞上一次就记住。不预先发：
                # OpenAI 还要求带上它自己那条 reasoning 条目的 id，我们只留了纯文本。
                # 只在补一条空思考真能改变请求体时才重试，否则原样再打一遍是白花一次调用。
                if (status_code == 400 and not self._echo_reasoning and "reasoning" in detail
                        and any(m.role == "assistant" and m.tool_calls and not m.reasoning for m in messages)):
                    self._echo_reasoning = True
                    payload["input"] = to_input(messages, echo_reasoning=True)
                    continue
                code = f"http_{status_code}"
                self._record_failure(code)
                raise LLMProviderError(code, f"{self._provider} 请求失败（HTTP {status_code}）{detail}", retryable=retryable) from error
            except httpx.RequestError as error:
                if emitted:
                    # 已输出增量后不重放整轮，避免重复文本（架构 §5.8）。
                    self._record_failure("stream_interrupted")
                    raise LLMProviderError("stream_interrupted", f"{self._provider} 流式响应中断", retryable=False) from error
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                self._record_failure("network_error")
                raise LLMProviderError("network_error", f"暂时无法连接 {self._provider}", retryable=True) from error
            except json.JSONDecodeError as error:
                code = "stream_interrupted" if emitted else "invalid_response"
                self._record_failure(code)
                raise LLMProviderError(code, f"{self._provider} 返回了无法解析的流式数据", retryable=False) from error

    @staticmethod
    def _key(calls: dict[object, dict[str, str]], aliases: dict[object, object], chunk: dict) -> object:
        # 事件按 output_index 归位，缺了退到 item_id，两者都没有就拼到最近一个槽——
        # 否则两个并行调用会落进同一个槽，arguments 拼成非法 JSON。
        key = chunk.get("output_index")
        if key is None:
            key = chunk.get("item_id")
        if key is None:
            key = next(reversed(calls)) if calls else 0
        return aliases.get(key, key)

    def _slot(self, calls: dict[object, dict[str, str]], aliases: dict[object, object], chunk: dict) -> dict[str, str]:
        return calls.setdefault(self._key(calls, aliases, chunk), {"id": "", "name": "", "arguments": ""})

    def _absorb_item(self, calls: dict[object, dict[str, str]], aliases: dict[object, object],
                     chunk: dict, *, final: bool) -> None:
        """条目级事件带着 call_id 与工具名，参数只在收尾条目上是全量。"""
        item = chunk.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return
        key = self._key(calls, aliases, chunk)
        entry = calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
        # 条目事件报 output_index、参数增量只报 item_id 是常见混用，登记成同一个槽的别名；
        # 不登记的话一次调用会被劈成两条同 call_id 的残缺调用。
        for alias in (chunk.get("output_index"), chunk.get("item_id"), item.get("id")):
            if alias is not None and alias != key:
                aliases[alias] = key
        if item.get("call_id"):
            entry["id"] = str(item["call_id"])
        if item.get("name"):
            entry["name"] = str(item["name"])
        arguments = item.get("arguments")
        if isinstance(arguments, str) and arguments and (final or not entry["arguments"]):
            entry["arguments"] = arguments

    @staticmethod
    def _mark_search(searches: dict[str, dict[str, object]], call_id: str, *, done: bool) -> None:
        """状态事件只用来记时：厂商端一次搜索能跑几十秒，这是唯一的耗时来源。"""
        if not call_id:
            return
        entry = searches.setdefault(call_id, {"started": time.monotonic()})
        if done:
            entry["duration_ms"] = int((time.monotonic() - float(entry["started"])) * 1000)

    @staticmethod
    def _absorb_search(searches: dict[str, dict[str, object]], item: dict, *, final: bool) -> None:
        """条目事件带着「做了什么」与成没成，起始条目上这两样都还没有。"""
        call_id = str(item.get("id") or "")
        if not call_id:
            return
        entry = searches.setdefault(call_id, {"started": time.monotonic()})
        if final:
            entry["item"] = item
            entry.setdefault("duration_ms", int((time.monotonic() - float(entry["started"])) * 1000))

    @staticmethod
    def _recover(output: object, parts: list[str], calls: dict[object, dict[str, str]],
                 searches: dict[str, dict[str, object]] | None = None) -> list[str]:
        """只在增量缺席时从终态 response.output 里补：有的服务只发条目级事件。
        补出来的正文照样以增量发出去，上层拼答案的口径不变。"""
        recovered: list[str] = []
        known = {entry["id"] for entry in calls.values()}
        for index, item in enumerate(output if isinstance(output, list) else []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" and not parts:
                recovered += [part["text"] for part in item.get("content") or []
                              if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]]
            elif item.get("type") == _SERVER_SEARCH_ITEM and searches is not None:
                searches.setdefault(str(item.get("id") or index), {"started": time.monotonic()})["item"] = item
            elif item.get("type") == "function_call" and str(item.get("call_id") or "") not in known:
                calls[f"output_{index}"] = {"id": str(item.get("call_id") or ""),
                                            "name": str(item.get("name") or ""),
                                            "arguments": str(item.get("arguments") or "")}
        return recovered

    @staticmethod
    def _usage(raw: object) -> dict[str, int]:
        """Responses 的用量字段名与 Chat Completions 不同，这里换成同一套内部键名，
        上层统计与展示不必分协议。"""
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for source, target in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"),
                               ("total_tokens", "total_tokens")):
            value = raw.get(source)
            if isinstance(value, int):
                result[target] = value
        for parent, key in (("input_tokens_details", "cached_tokens"), ("output_tokens_details", "reasoning_tokens")):
            details = raw.get(parent)
            if isinstance(details, dict) and isinstance(details.get(key), int):
                result[key] = details[key]
        return result
