from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Sequence

import httpx

from contracts.llm import (
    ChatDelta,
    ChatFinal,
    ChatMessage,
    ChatToolCalls,
    LLMProviderError,
    ToolCallRequest,
    ToolSpec,
)


class DeepSeekAgentChat:
    """DeepSeek Chat Completions adapter：多轮 messages + function calling，流式输出。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        connect_timeout_seconds: float = 10,
        total_timeout_seconds: float = 180,
        max_output_tokens: int = 8192,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not base_url or not model:
            raise ValueError("DeepSeek adapter requires api_key, base_url and model")
        self._model = model
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._max_output_tokens = max(256, max_output_tokens)
        self._max_retries = max(0, max_retries)
        self._owns_client = client is None
        timeout = httpx.Timeout(total_timeout_seconds, connect=connect_timeout_seconds)
        self._client = client or httpx.Client(timeout=timeout)
        self._state_lock = threading.Lock()
        self._last_call_ok: bool | None = None
        self._last_error_code: str | None = None

    @property
    def mode(self) -> str:
        return "provider"

    @property
    def provider(self) -> str:
        return "deepseek"

    @property
    def model(self) -> str:
        return self._model

    def chat(self, *, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> Iterator[ChatDelta | ChatToolCalls | ChatFinal]:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": self._to_wire(messages),
            # DeepSeek V4 enables thinking by default. Tutor turns use the
            # lower-latency non-thinking path unless a future policy opts in.
            "thinking": {"type": "disabled"},
            "max_tokens": self._max_output_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
                for tool in tools
            ]
        attempt = 0
        while True:
            emitted = False
            try:
                with self._client.stream("POST", self._endpoint, json=payload, headers=self._headers) as response:
                    response.raise_for_status()
                    parts: list[str] = []
                    tool_calls_acc: dict[int, dict[str, str]] = {}
                    finish_reason, usage = "stop", {}
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
                        for raw_call in delta.get("tool_calls") or []:
                            self._accumulate_tool_call(tool_calls_acc, raw_call)
                        if choices[0].get("finish_reason"):
                            finish_reason = str(choices[0]["finish_reason"])
                    if tool_calls_acc:
                        calls = tuple(
                            ToolCallRequest(id=acc["id"] or f"call_{index}", name=acc["name"], arguments=acc["arguments"])
                            for index, acc in sorted(tool_calls_acc.items())
                        )
                        self._record_success()
                        yield ChatToolCalls(calls=calls, usage=usage)
                        return
                    text = "".join(parts).strip()
                    if not text:
                        self._record_failure("invalid_response")
                        raise LLMProviderError("invalid_response", "DeepSeek 返回了空回答", retryable=False)
                    self._record_success()
                    yield ChatFinal(text=text, finish_reason=finish_reason, provider=self.provider, model=self.model, mode=self.mode, usage=usage)
                    return
            except httpx.HTTPStatusError as error:
                # raise_for_status 在拿到任何 delta 之前触发，可以安全重试。
                status = error.response.status_code
                retryable = status == 429 or status >= 500
                if retryable and attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                code = f"http_{status}"
                self._record_failure(code)
                raise LLMProviderError(code, f"DeepSeek 请求失败（HTTP {status}）", retryable=retryable) from error
            except httpx.RequestError as error:
                if emitted:
                    # 已输出增量后不重放整轮，避免重复文本（架构 §5.8）。
                    self._record_failure("stream_interrupted")
                    raise LLMProviderError("stream_interrupted", "DeepSeek 流式响应中断", retryable=False) from error
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                self._record_failure("network_error")
                raise LLMProviderError("network_error", "暂时无法连接 DeepSeek", retryable=True) from error
            except json.JSONDecodeError as error:
                code = "stream_interrupted" if emitted else "invalid_response"
                self._record_failure(code)
                raise LLMProviderError(code, "DeepSeek 返回了无法解析的流式数据", retryable=False) from error

    def health(self) -> dict[str, object]:
        with self._state_lock:
            last_call_ok, last_error_code = self._last_call_ok, self._last_error_code
        return {
            "configured": True,
            "enabled": True,
            "mode": self.mode,
            "adapter_available": True,
            "provider": self.provider,
            "model": self.model,
            "last_call_ok": last_call_ok,
            "last_error_code": last_error_code,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @staticmethod
    def _accumulate_tool_call(acc: dict[int, dict[str, str]], raw_call: dict) -> None:
        # 流式 tool_calls 按 index 分片下发，逐片拼接 id/name/arguments。
        index = raw_call.get("index", 0)
        entry = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if raw_call.get("id"):
            entry["id"] = raw_call["id"]
        function = raw_call.get("function") or {}
        if function.get("name"):
            entry["name"] = function["name"]
        if function.get("arguments"):
            entry["arguments"] += function["arguments"]

    @staticmethod
    def _to_wire(messages: Sequence[ChatMessage]) -> list[dict[str, object]]:
        wire: list[dict[str, object]] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                wire.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
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
        return result

    def _record_success(self) -> None:
        with self._state_lock:
            self._last_call_ok = True
            self._last_error_code = None

    def _record_failure(self, code: str) -> None:
        with self._state_lock:
            self._last_call_ok = False
            self._last_error_code = code
