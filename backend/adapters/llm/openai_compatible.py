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
    ToolCallRequest,
    ToolSpec,
)

from .http_chat import HttpChatBase


class OpenAICompatibleChat(HttpChatBase):
    """OpenAI Chat Completions 兼容适配器：多轮 messages + function calling，流式输出。

    只用标准字段，任何兼容该协议的服务都能接。厂商私有参数走 extra_body 传入。
    """

    endpoint_path = "/chat/completions"
    protocol_fields = {name: "它是协议字段，覆盖了流式解析会崩在运行时"
                       for name in ("model", "messages", "stream", "tools", "max_tokens")}
    default_provider = "openai_compatible"
    # 撞过一次「必须回传 reasoning_content」之后，这个实例就一直带上该字段。
    _echo_reasoning = False

    def chat(self, *, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> Iterator[ChatDelta | ChatReasoning | ChatToolCalls | ChatFinal]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": self._to_wire(messages),
            "max_tokens": self._max_output_tokens,
            "stream": True,
            # 不带这个的话部分服务流式 usage 恒为 null，token 统计静默丢失。
            "stream_options": {"include_usage": True},
            **self._extra_body,
        }
        # 推理系模型要求 max_completion_tokens 且拒绝 max_tokens 同时出现，两者互斥。
        if payload.get("max_completion_tokens") is not None:
            payload.pop("max_tokens", None)
        # extra_body 里置 null 表示移除该字段，留给不认识 stream_options 的服务一个出口。
        payload = {key: value for key, value in payload.items() if value is not None}
        if tools:
            payload["tools"] = [tool.wire() for tool in tools]
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
                    tool_calls_acc: dict[int, dict[str, str]] = {}
                    finish_reason, usage = "stop", {}
                    # 厂商到底返回了什么，与上面那个兜底成 stop 的分开留：
                    # 混成一个就分不出「厂商说 stop」和「厂商一个字没说」。
                    raw_finish_reason: str | None = None
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        usage.update(self._usage(chunk.get("usage")))
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str) and piece:
                            emitted = True
                            parts.append(piece)
                            yield ChatDelta(piece)
                        # 思考内容单独走一路：它不进答案，但要回传给厂商。
                        # 刻意不设 emitted——答案还没开始，网络抖动时整轮重试仍然安全。
                        # 字段名不统一：reasoning_content 之外也有服务用 reasoning。
                        # 实际用的是哪个名字随增量带出去，开发者模式要显示它。
                        thinking_field = "reasoning_content" if delta.get("reasoning_content") else "reasoning"
                        thinking = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(thinking, str) and thinking:
                            reasoning_parts.append(thinking)
                            yield ChatReasoning(thinking, field=thinking_field)
                        for raw_call in delta.get("tool_calls") or []:
                            self._accumulate_tool_call(tool_calls_acc, raw_call)
                        if choices[0].get("finish_reason"):
                            raw_finish_reason = str(choices[0]["finish_reason"])
                            finish_reason = raw_finish_reason
                    if tool_calls_acc:
                        calls = tuple(
                            ToolCallRequest(id=acc["id"] or f"call_{index}", name=acc["name"], arguments=acc["arguments"])
                            for index, acc in sorted(tool_calls_acc.items())
                        )
                        self._record_success()
                        yield ChatToolCalls(calls=calls, usage=usage, reasoning="".join(reasoning_parts),
                                            provider_finish_reason=raw_finish_reason)
                        return
                    text = "".join(parts).strip()
                    if not text:
                        # 空回答按收尾原因归类：推理模型思考吃完输出预算是常见态，
                        # 一律报「空回答」会让人查错方向。
                        code, why = {
                            "length": ("output_truncated", "输出在生成完正文前达到 token 上限"),
                            "content_filter": ("content_filtered", "输出被服务端内容过滤拦下"),
                        }.get(finish_reason, ("invalid_response", "返回了空回答"))
                        self._record_failure(code)
                        raise LLMProviderError(code, f"{self._provider} {why}", retryable=False)
                    self._record_success()
                    yield ChatFinal(text=text, finish_reason=finish_reason, provider=self.provider, model=self.model,
                                    mode=self.mode, usage=usage, provider_finish_reason=raw_finish_reason)
                    return
            except httpx.HTTPStatusError as error:
                # raise_for_status 在拿到任何 delta 之前触发，可以安全重试。
                status = error.response.status_code
                retryable = status == 429 or status >= 500
                if retryable and attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                detail = self._detail(error.response)
                # 思考模式要求 assistant 消息回传 reasoning_content，只校验字段在不在，空串就行。
                # 不预先发这个字段：它是厂商扩展，对不认识它的服务发过去可能被拒。撞上一次就记住。
                if status == 400 and not self._echo_reasoning and "reasoning_content" in detail:
                    self._echo_reasoning = True
                    payload["messages"] = self._to_wire(messages)
                    continue
                code = f"http_{status}"
                self._record_failure(code)
                raise LLMProviderError(code, f"{self._provider} 请求失败（HTTP {status}）{detail}", retryable=retryable) from error
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
    def _accumulate_tool_call(acc: dict[int, dict[str, str]], raw_call: dict) -> None:
        # 流式 tool_calls 按 index 分片下发，逐片拼接 id/name/arguments。
        # 有的服务不发 index：带 id 的分片当作新调用开槽，不带的拼到最近一个——
        # 否则两个并行调用会落进同一个槽，arguments 拼成非法 JSON。
        index = raw_call.get("index")
        if index is None:
            index = len(acc) if raw_call.get("id") or not acc else max(acc)
        entry = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if raw_call.get("id"):
            entry["id"] = raw_call["id"]
        function = raw_call.get("function") or {}
        if function.get("name"):
            entry["name"] = function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]

    def _to_wire(self, messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
        wire: list[dict[str, object]] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        # 思考模式下这个字段必须在。种子检索那条 assistant 消息是服务端构造的，
                        # 本来就没有思考内容，给空串即可——厂商只校验字段存在。
                        **({"reasoning_content": message.reasoning} if message.reasoning or self._echo_reasoning else {}),
                        "tool_calls": [
                            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                            for call in message.tool_calls
                        ],
                    }
                )
            elif message.role == "tool":
                wire.append({"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id or ""})
            else:
                wire.append({"role": message.role, "content": message.content})
        return wire

    @staticmethod
    def _usage(raw: object) -> dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        result: dict[str, int] = {}
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            value = raw.get(key)
            if isinstance(value, int):
                result[key] = value
        # 缓存与思考的明细有的服务放嵌套对象里，拍平收进来，有就记没有跳过。
        for parent, key in (("prompt_tokens_details", "cached_tokens"), ("completion_tokens_details", "reasoning_tokens")):
            details = raw.get(parent)
            if isinstance(details, dict) and isinstance(details.get(key), int):
                result[key] = details[key]
        return result
