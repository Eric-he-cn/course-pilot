from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from adapters.llm.openai_compatible import OpenAICompatibleChat
from contracts.llm import ChatDelta, ChatFinal, ChatMessage, ChatReasoning, ChatToolCalls, LLMProviderError, ToolCallRequest, ToolSpec

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


def _adapter(client: httpx.Client, *, max_retries: int = 0) -> OpenAICompatibleChat:
    return OpenAICompatibleChat(
        api_key="test-secret",
        base_url="https://api.example.com/v1",
        model="example-model",
        max_output_tokens=2048,
        max_retries=max_retries,
        client=client,
    )


def test_reasoning_is_streamed_apart_from_the_answer_and_passed_back():
    """思考内容走 reasoning_content：只读 content 的话思考期间一个字都拿不到；
    而且它必须随 assistant 消息回传，缺了厂商会拒收整轮（真实踩过的 HTTP 400）。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": None, "reasoning_content": "先看看教材"}}]},
            {"choices": [{"delta": {"content": None, "reasoning_content": "再决定查什么"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search_materials", "arguments": "{}"}}]}}]},
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    thoughts = [i.text for i in items if isinstance(i, ChatReasoning)]
    assert thoughts == ["先看看教材", "再决定查什么"]
    assert not [i for i in items if isinstance(i, ChatDelta)], "思考内容不该混进答案文本"
    calls = items[-1]
    assert isinstance(calls, ChatToolCalls)
    assert calls.reasoning == "先看看教材再决定查什么"

    # 回传：assistant 历史消息里要带上 reasoning_content
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(_adapter(client).chat(messages=[
            *_messages(),
            ChatMessage(role="assistant", content="", tool_calls=calls.calls, reasoning=calls.reasoning),
            ChatMessage(role="tool", content="检索结果", tool_call_id="c1"),
        ]))
    assistant = [m for m in bodies[-1]["messages"] if m["role"] == "assistant"][0]
    assert assistant["reasoning_content"] == "先看看教材再决定查什么"


def test_reasoning_echo_is_learned_from_the_rejection_not_sent_upfront():
    """思考模式要求 assistant 消息带 reasoning_content，只校验字段存在（空串就行）。
    但这是厂商扩展字段，预先发给不认识它的服务可能被拒——所以撞上那个 400 才补，然后记住。
    种子检索那条 assistant 消息是服务端构造的，本来就没有思考内容，正是靠这条路径过。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assistant = next((m for m in body["messages"] if m["role"] == "assistant"), None)
        if assistant is not None and "reasoning_content" not in assistant:
            return httpx.Response(400, json={"error": {"message": "The `reasoning_content` in the thinking mode must be passed back to the API."}})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))

    seeded = [
        *_messages(),
        ChatMessage(role="assistant", content="", tool_calls=(ToolCallRequest(id="seed", name="search_materials", arguments="{}"),)),
        ChatMessage(role="tool", content="[1] 片段", tool_call_id="seed"),
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        final = list(adapter.chat(messages=seeded))[-1]
        assert isinstance(final, ChatFinal)
        assert len(bodies) == 2, "第一次不带字段、被拒后补上重试"
        assert "reasoning_content" not in bodies[0]["messages"][2]
        assert bodies[1]["messages"][2]["reasoning_content"] == ""

        # 学到之后同一个适配器实例直接带上，不再白撞一次
        list(adapter.chat(messages=seeded))
        assert bodies[2]["messages"][2]["reasoning_content"] == ""


def test_reasoning_field_stays_absent_for_providers_that_never_ask():
    """没要求过这个字段的服务，一次都不该收到它。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(_adapter(client).chat(messages=[
            *_messages(),
            ChatMessage(role="assistant", content="", tool_calls=(ToolCallRequest(id="seed", name="search_materials", arguments="{}"),)),
            ChatMessage(role="tool", content="[1] 片段", tool_call_id="seed"),
        ]))
    assert all("reasoning_content" not in m for body in bodies for m in body["messages"])


def test_provider_error_carries_the_server_explanation():
    """4xx 只有状态码等于没有线索——是模型名错了还是参数不被接受，全靠服务端这句话。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "The `reasoning_content` must be passed back."}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))
    assert caught.value.code == "http_400"
    assert "reasoning_content" in str(caught.value)


def test_extra_body_is_merged_and_cannot_break_the_protocol():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleChat(
            api_key="k", base_url="https://api.example.com/v1", model="m", provider="vendor-x",
            extra_body={"thinking": {"type": "disabled"}, "reasoning_effort": "low"}, client=client,
        )
        final = list(adapter.chat(messages=_messages()))[-1]

    body = captured["body"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["reasoning_effort"] == "low"
    assert body["stream"] is True
    assert isinstance(final, ChatFinal) and final.provider == "vendor-x"

    # 覆盖协议字段会让流式解析崩在运行时，构造期就拦掉。
    with pytest.raises(ValueError, match="messages"):
        OpenAICompatibleChat(api_key="k", base_url="https://api.example.com/v1", model="m",
                             extra_body={"messages": [], "stream": False})


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

    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "example-model"
    assert body["max_tokens"] == 2048
    assert body["stream"] is True
    # 默认只发标准字段，任何厂商私有参数都不该凭空出现在请求里。
    assert set(body) == {"model", "messages", "max_tokens", "stream", "stream_options", "tools"}
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
    """服务端对错误的说明要带上（不然 4xx 无从下手），但密钥一律抹掉——
    401 的消息里回显 key 是真实存在的情况。响应体其余部分不进错误消息。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": {"message": "Invalid api key: test-secret"},
            "debug": "sensitive-provider-body",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as raised:
            list(adapter.chat(messages=_messages()))
        assert raised.value.code == "http_401"
        assert raised.value.retryable is False
        assert "test-secret" not in str(raised.value)
        assert "***" in str(raised.value)
        assert "sensitive-provider-body" not in str(raised.value)
        assert adapter.health()["last_error_code"] == "http_401"


def test_max_completion_tokens_alias_suppresses_max_tokens():
    """推理系模型要求 max_completion_tokens 且拒绝 max_tokens 同时出现。
    extra_body 给了新名字就不发旧名字，两者互斥；直接覆盖 max_tokens 仍然构造期报错。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleChat(
            api_key="k", base_url="https://api.example.com/v1", model="m",
            extra_body={"max_completion_tokens": 1024}, client=client,
        )
        list(adapter.chat(messages=_messages()))

    body = captured["body"]
    assert body["max_completion_tokens"] == 1024
    assert "max_tokens" not in body

    with pytest.raises(ValueError, match="max_tokens"):
        OpenAICompatibleChat(api_key="k", base_url="https://api.example.com/v1", model="m",
                             extra_body={"max_tokens": 1024})


def test_tool_call_fragments_without_index_split_by_id():
    """有的服务流式 tool_calls 不带 index。带 id 的分片当作新调用开槽，
    不带的拼到最近一个——否则两个并行调用会拼成一份非法 JSON。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"tool_calls": [{"id": "call_a", "function": {"name": "search_materials", "arguments": "{\"query\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "\"链式法则\"}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"id": "call_b", "function": {"name": "calculator", "arguments": "{\"expression\":\"1+1\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert [call.id for call in result.calls] == ["call_a", "call_b"]
    assert json.loads(result.calls[0].arguments) == {"query": "链式法则"}
    assert json.loads(result.calls[1].arguments) == {"expression": "1+1"}


def test_reasoning_falls_back_to_the_shorter_field_name():
    """思考内容的字段名不统一：reasoning_content 之外也有服务用 reasoning。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"reasoning": "先想一下"}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        events = list(_adapter(client).chat(messages=_messages()))

    assert any(isinstance(event, ChatReasoning) and event.text == "先想一下" for event in events)


def test_stream_usage_is_requested_and_nested_details_are_read():
    """不带 stream_options 的话部分服务流式 usage 恒为 null，统计静默丢失；
    嵌套的 cache/reasoning 明细拍平收进 usage。extra_body 置 null 可整个移除该字段。"""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                       "prompt_tokens_details": {"cached_tokens": 8},
                                       "completion_tokens_details": {"reasoning_tokens": 3}}},
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages()))[-1]

    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert isinstance(final, ChatFinal)
    assert final.usage["cached_tokens"] == 8 and final.usage["reasoning_tokens"] == 3

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatibleChat(api_key="k", base_url="https://api.example.com/v1", model="m",
                                       extra_body={"stream_options": None}, client=client)
        list(adapter.chat(messages=_messages()))
    assert "stream_options" not in captured["body"]


def test_empty_answer_from_length_is_reported_as_truncated():
    """推理模型思考吃完输出预算时正文为空且 finish_reason=length，
    报「空回答」会让人查错方向，按截断归类。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"reasoning_content": "想了很久"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))

    assert caught.value.code == "output_truncated"


def test_error_detail_recognizes_top_level_shapes():
    """vLLM / FastAPI 系服务的错误不在 error.message 里，而是顶层 detail 或 message。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "unknown field: foo"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))

    assert "unknown field: foo" in str(caught.value)
