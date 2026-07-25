from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from adapters.llm.deepseek import DeepSeekAgentChat
from contracts.llm import ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, LLMProviderError, ToolCallRequest, ToolSpec

_TOOLS = (
    ToolSpec(
        name="search_materials",
        description="检索课程教材",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    ),
)


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="你是课程辅导老师。"),
        ChatMessage(role="user", content="链式法则怎么用？"),
    ]


def _sse(*chunks: object) -> bytes:
    frames = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _adapter(client: httpx.Client, *, max_retries: int = 0) -> DeepSeekAgentChat:
    return DeepSeekAgentChat(
        api_key="test-secret",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_output_tokens=2048,
        max_retries=max_retries,
        client=client,
    )


def test_streams_chat_completion_and_encodes_tools():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"choices": [{"delta": {"content": "先求外层，"}}]},
                {"choices": [{"delta": {"content": "再乘内层。[1]"}}]},
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                },
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer test-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 2048
    assert body["stream"] is True
    assert body["messages"][0]["role"] == "system"
    assert body["tools"][0]["function"]["name"] == "search_materials"
    assert body["tools"][0]["function"]["parameters"]["required"] == ["query"]

    deltas = [item for item in items if isinstance(item, ChatDelta)]
    assert [delta.text for delta in deltas] == ["先求外层，", "再乘内层。[1]"]
    final = items[-1]
    assert isinstance(final, ChatFinal)
    assert final.text == "先求外层，再乘内层。[1]"
    assert final.finish_reason == "stop"
    assert final.usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def test_accumulates_streamed_tool_call_fragments():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "search_materials", "arguments": ""}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"query":'}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "梯度下降"}'}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"total_tokens": 42}},
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    assert len(items) == 1
    calls = items[0]
    assert isinstance(calls, ChatToolCalls)
    assert calls.calls[0].id == "call_1"
    assert calls.calls[0].name == "search_materials"
    assert json.loads(calls.calls[0].arguments) == {"query": "梯度下降"}
    assert calls.usage == {"total_tokens": 42}


def test_encodes_assistant_tool_calls_and_tool_result_messages():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "好的"}, "finish_reason": "stop"}]}))

    messages = [
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="", tool_calls=(ToolCallRequest(id="c1", name="search_materials", arguments='{"query":"x"}'),)),
        ChatMessage(role="tool", content="检索到的证据", tool_call_id="c1"),
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(_adapter(client).chat(messages=messages))

    wire = captured["body"]["messages"]
    assert wire[2]["role"] == "assistant"
    assert wire[2]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {"name": "search_materials", "arguments": '{"query":"x"}'}}
    assert wire[3] == {"role": "tool", "content": "检索到的证据", "tool_call_id": "c1"}
    # tools 为空时不带 tools 字段，避免供应商拒绝空数组。
    assert "tools" not in captured["body"]


def test_empty_answer_is_reported_as_invalid_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as raised:
            list(adapter.chat(messages=_messages()))
        assert raised.value.code == "invalid_response"


def test_retries_before_first_delta_then_succeeds():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client, max_retries=1).chat(messages=_messages()))
    assert len(calls) == 2
    assert isinstance(items[-1], ChatFinal)
    assert items[-1].text == "答案"


def test_mid_stream_drop_is_reported_as_stream_interrupted_not_retried():
    calls: list[int] = []

    def dropping_stream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"\xe9\x93\xbe\xe5\xbc\x8f"}}]}\n\n'
        raise httpx.ReadError("connection lost")

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=dropping_stream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client, max_retries=2)
        stream = adapter.chat(messages=_messages())
        assert next(stream) == ChatDelta("链式")
        with pytest.raises(LLMProviderError) as raised:
            list(stream)
        assert raised.value.code == "stream_interrupted"
        assert len(calls) == 1  # 已输出 delta 后不得重放整轮
        assert adapter.health()["last_error_code"] == "stream_interrupted"


def test_provider_error_is_sanitized_and_recorded():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "sensitive-provider-body"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as raised:
            list(adapter.chat(messages=_messages()))
        assert raised.value.code == "http_401"
        assert raised.value.retryable is False
        assert "test-secret" not in str(raised.value)
        assert "sensitive-provider-body" not in str(raised.value)
        assert adapter.health()["last_error_code"] == "http_401"
