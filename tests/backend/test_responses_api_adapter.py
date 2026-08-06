"""Responses 协议适配器。假流按真机实测的事件序列构造（DeepSeek deepseek-v4-flash，
2026-08-06）：条目级事件 + 增量事件，收尾是 response.completed，没有 data: [DONE]。"""
from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from adapters.llm.responses_api import ResponsesApiChat, to_input, to_tools
from contracts.llm import (ChatDelta, ChatFinal, ChatMessage, ChatReasoning, ChatToolCalls,
                           LLMProviderError, ToolCallRequest, ToolSpec)

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


def _sse(*events: object) -> bytes:
    """Responses 的每帧都带 event 行；流以收尾事件结束，没有 [DONE] 哨兵。"""
    frames = [f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events]
    return "".join(frames).encode("utf-8")


def _completed(**response: object) -> dict[str, object]:
    return {"type": "response.completed", "response": {"status": "completed", "output": [], **response}}


def _text(delta: str) -> dict[str, object]:
    return {"type": "response.output_text.delta", "delta": delta, "item_id": "msg_1", "output_index": 1}


def _adapter(client: httpx.Client, *, max_retries: int = 0) -> ResponsesApiChat:
    return ResponsesApiChat(
        api_key="test-secret",
        base_url="https://api.example.com",
        model="example-model",
        max_output_tokens=2048,
        max_retries=max_retries,
        client=client,
    )


# ---- 请求形状 ----

def test_streams_text_and_encodes_the_request():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(
            _text("先求外层，"),
            _text("再乘内层。[1]"),
            _completed(usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    assert captured["url"] == "https://api.example.com/responses"
    assert captured["authorization"] == "Bearer test-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    # 默认只发标准字段，任何厂商私有参数都不该凭空出现在请求里。
    assert set(body) == {"model", "input", "max_output_tokens", "stream", "store", "tools"}
    assert body["model"] == "example-model"
    assert body["max_output_tokens"] == 2048
    assert body["stream"] is True
    # 无状态调用：不在厂商侧留会话记录。
    assert body["store"] is False
    # 工具定义是平铺的，没有 Chat Completions 那层 function 嵌套。
    assert body["tools"][0] == {"type": "function", "name": "search_materials", "description": "检索课程教材",
                                "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                                               "required": ["query"]}}

    assert [item.text for item in items if isinstance(item, ChatDelta)] == ["先求外层，", "再乘内层。[1]"]
    final = items[-1]
    assert isinstance(final, ChatFinal)
    assert final.text == "先求外层，再乘内层。[1]"
    assert final.finish_reason == "stop"
    # 用量换成内部键名，上层统计不必分协议。
    assert final.usage == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}


def test_tools_are_absent_when_none_are_offered():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(_text("好"), _completed()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(_adapter(client).chat(messages=_messages()))
    assert "tools" not in captured["body"]


# ---- 消息与工具的纯函数转换 ----

def test_messages_become_input_items():
    items = to_input([
        ChatMessage(role="system", content="s"),
        ChatMessage(role="user", content="q"),
        ChatMessage(role="assistant", content="我先查一下",
                    tool_calls=(ToolCallRequest(id="c1", name="search_materials", arguments='{"query":"x"}'),)),
        ChatMessage(role="tool", content="检索到的证据", tool_call_id="c1"),
        ChatMessage(role="assistant", content="结论"),
    ])
    assert items == [
        {"type": "message", "role": "system", "content": "s"},
        {"type": "message", "role": "user", "content": "q"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "我先查一下"}]},
        {"type": "function_call", "call_id": "c1", "name": "search_materials", "arguments": '{"query":"x"}'},
        {"type": "function_call_output", "call_id": "c1", "output": "检索到的证据"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "结论"}]},
    ]


def test_an_assistant_turn_with_no_text_contributes_only_the_calls():
    """种子检索那条 assistant 消息正文是空的，不该发一个空的 message 条目。"""
    items = to_input([ChatMessage(role="assistant", content="",
                                  tool_calls=(ToolCallRequest(id="seed", name="search_materials", arguments="{}"),))])
    assert items == [{"type": "function_call", "call_id": "seed", "name": "search_materials", "arguments": "{}"}]


def test_an_empty_assistant_message_contributes_nothing():
    """空正文不发条目——带不带工具调用的两支要一致。"""
    assert to_input([ChatMessage(role="assistant", content="")]) == []


def test_reasoning_becomes_its_own_item_ahead_of_the_calls():
    items = to_input([ChatMessage(role="assistant", content="", reasoning="先看看教材",
                                  tool_calls=(ToolCallRequest(id="c1", name="search_materials", arguments="{}"),))])
    assert items[0] == {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "先看看教材"}]}
    assert items[1]["type"] == "function_call"


def test_tool_specs_are_flattened():
    assert to_tools(_TOOLS) == [{"type": "function", "name": "search_materials", "description": "检索课程教材",
                                 "parameters": _TOOLS[0].parameters}]


# ---- 工具调用 ----

def test_accumulates_streamed_tool_call_fragments():
    """真机的顺序：output_item.added 带 call_id 与工具名，参数分片跟在后面。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search_materials", "arguments": ""}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_1", "delta": '{"query":'},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_1", "delta": ' "梯度下降"}'},
            {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc_1",
             "arguments": '{"query": "梯度下降"}'},
            _completed(usage={"total_tokens": 42}),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    assert len(items) == 1
    calls = items[0]
    assert isinstance(calls, ChatToolCalls)
    assert calls.calls[0].id == "call_1"
    assert calls.calls[0].name == "search_materials"
    assert json.loads(calls.calls[0].arguments) == {"query": "梯度下降"}
    assert calls.usage == {"total_tokens": 42}


def test_parallel_tool_calls_stay_in_their_own_slots():
    """两个并行调用按 output_index 分开，否则 arguments 拼成一份非法 JSON。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "function_call", "id": "fc_a", "call_id": "call_a", "name": "search_materials"}},
            {"type": "response.output_item.added", "output_index": 1, "item": {
                "type": "function_call", "id": "fc_b", "call_id": "call_b", "name": "calculator"}},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_a", "delta": '{"query":'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_b", "delta": '{"expression":'},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_a", "delta": '"链式法则"}'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "fc_b", "delta": '"1+1"}'},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert [call.id for call in result.calls] == ["call_a", "call_b"]
    assert json.loads(result.calls[0].arguments) == {"query": "链式法则"}
    assert json.loads(result.calls[1].arguments) == {"expression": "1+1"}


def test_the_stream_is_not_read_past_the_terminal_event():
    """服务端收尾后不关流是常见的，接着读会卡到超时、把已经收全的答案整个丢掉。"""
    def stalling_stream() -> Iterator[bytes]:
        yield _sse(_text("答案"), _completed())
        raise httpx.ReadError("服务端收尾后没关流，这里会读到超时")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stalling_stream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages()))[-1]

    assert isinstance(final, ChatFinal) and final.text == "答案"


def test_events_arriving_after_the_terminal_one_are_not_appended():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("答案"), _completed(), _text("迟到的一段")))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages()))

    assert [item.text for item in items if isinstance(item, ChatDelta)] == ["答案"]


def test_one_call_reported_under_two_identifiers_stays_one_call():
    """条目事件报 output_index、参数增量只报 item_id 是常见混用。不认这是同一次调用的话，
    一次调用会被劈成两条同 call_id 的残缺调用——call_id 唯一是 trace 的前提。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.added", "output_index": 0, "item": {
                "type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "search_materials"}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": '{"query":"x"}'},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert len(result.calls) == 1
    assert result.calls[0].name == "search_materials"
    assert json.loads(result.calls[0].arguments) == {"query": "x"}


def test_calls_are_ordered_by_output_index_not_by_arrival():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.done", "output_index": 1, "item": {
                "type": "function_call", "call_id": "call_B", "name": "second", "arguments": "{}"}},
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "function_call", "call_id": "call_A", "name": "first", "arguments": "{}"}},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert [call.name for call in result.calls] == ["first", "second"]


def test_an_empty_arguments_fragment_does_not_conjure_a_call():
    """空增量开了槽的话，一次正常回答会被当成工具调用轮，正文整段顶掉。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            _text("这是正常回答"),
            {"type": "response.function_call_arguments.delta", "delta": ""},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(final, ChatFinal) and final.text == "这是正常回答"


def test_fragments_without_output_index_fall_back_to_item_id():
    """没有 output_index 的服务靠 item_id 归位，两个调用照样分得开。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.added", "item_id": "fc_a", "item": {
                "type": "function_call", "call_id": "call_a", "name": "search_materials"}},
            {"type": "response.output_item.added", "item_id": "fc_b", "item": {
                "type": "function_call", "call_id": "call_b", "name": "calculator"}},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_a", "delta": '{"query":"x"}'},
            {"type": "response.function_call_arguments.delta", "item_id": "fc_b", "delta": '{"expression":"1+1"}'},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert [call.id for call in result.calls] == ["call_a", "call_b"]
    assert json.loads(result.calls[0].arguments) == {"query": "x"}
    assert json.loads(result.calls[1].arguments) == {"expression": "1+1"}


def test_a_service_that_only_sends_item_events_still_yields_the_call():
    """参数只在收尾条目上给全量：增量一片没发也要能拿到完整调用。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "function_call", "call_id": "call_1", "name": "search_materials",
                "arguments": '{"query":"梯度下降"}'}},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert json.loads(result.calls[0].arguments) == {"query": "梯度下降"}


def test_the_terminal_output_backfills_when_no_incremental_event_arrived():
    """只在收尾一次性给结果的服务：正文照样以增量发出去，工具调用也补齐。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_completed(output=[
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "想了想"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "答案在这里"}]},
        ])))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages()))

    assert [item.text for item in items if isinstance(item, ChatDelta)] == ["答案在这里"]
    assert isinstance(items[-1], ChatFinal) and items[-1].text == "答案在这里"


def test_the_terminal_output_does_not_duplicate_text_already_streamed():
    """真机的 response.completed 里也带着完整正文，收过增量就不能再收一遍。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("答案"), _text("在这里"), _completed(output=[
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "答案在这里"}]},
            {"type": "function_call", "call_id": "call_1", "name": "search_materials", "arguments": "{}"},
        ])))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    assert [item.text for item in items if isinstance(item, ChatDelta)] == ["答案", "在这里"]
    calls = items[-1]
    assert isinstance(calls, ChatToolCalls)
    assert [call.id for call in calls.calls] == ["call_1"]


def test_a_call_already_seen_is_not_added_twice_by_the_terminal_output():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "function_call", "call_id": "call_1", "name": "search_materials", "arguments": "{}"}},
            _completed(output=[{"type": "function_call", "call_id": "call_1", "name": "search_materials",
                                "arguments": "{}"}]),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(result, ChatToolCalls)
    assert len(result.calls) == 1


# ---- 思考内容 ----

def test_reasoning_is_streamed_apart_from_the_answer_and_passed_back():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(
            {"type": "response.reasoning_text.delta", "delta": "先看看教材", "output_index": 0},
            {"type": "response.reasoning_text.delta", "delta": "再决定查什么", "output_index": 0},
            {"type": "response.output_item.done", "output_index": 1, "item": {
                "type": "function_call", "call_id": "c1", "name": "search_materials", "arguments": "{}"}},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))

    assert [item.text for item in items if isinstance(item, ChatReasoning)] == ["先看看教材", "再决定查什么"]
    assert not [item for item in items if isinstance(item, ChatDelta)], "思考内容不该混进答案文本"
    calls = items[-1]
    assert isinstance(calls, ChatToolCalls)
    assert calls.reasoning == "先看看教材再决定查什么"

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(_adapter(client).chat(messages=[
            *_messages(),
            ChatMessage(role="assistant", content="", tool_calls=calls.calls, reasoning=calls.reasoning),
            ChatMessage(role="tool", content="检索结果", tool_call_id="c1"),
        ]))
    reasoning = [item for item in bodies[-1]["input"] if item["type"] == "reasoning"]
    assert reasoning == [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "先看看教材再决定查什么"}]}]


def test_the_reasoning_event_name_is_reported_as_the_field():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.reasoning_text.delta", "delta": "先想一下", "output_index": 0},
            _text("好"), _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages()))

    assert [item.field for item in items if isinstance(item, ChatReasoning)] == ["reasoning_text"]


def test_a_summary_only_service_reports_the_other_reasoning_route():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.reasoning_summary_text.delta", "delta": "概括一下", "output_index": 0},
            _text("好"), _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages()))

    assert [item.field for item in items if isinstance(item, ChatReasoning)] == ["reasoning_summary_text"]


def test_reasoning_echo_is_learned_from_the_rejection_not_sent_upfront():
    """思考内容是否必须回传因服务而异，撞上 400 才补——预先发给不认识它的服务可能被拒。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if not any(item["type"] == "reasoning" for item in body["input"]):
            return httpx.Response(400, json={"error": {"message": "the reasoning item must be passed back"}})
        return httpx.Response(200, content=_sse(_text("好"), _completed()))

    seeded = [
        *_messages(),
        ChatMessage(role="assistant", content="", tool_calls=(ToolCallRequest(id="seed", name="search_materials", arguments="{}"),)),
        ChatMessage(role="tool", content="[1] 片段", tool_call_id="seed"),
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        final = list(adapter.chat(messages=seeded))[-1]
        assert isinstance(final, ChatFinal)
        assert len(bodies) == 2, "第一次不带条目、被拒后补上重试"
        assert not any(item["type"] == "reasoning" for item in bodies[0]["input"])
        assert {"type": "reasoning", "content": [{"type": "reasoning_text", "text": ""}]} in bodies[1]["input"]

        # 学到之后同一个适配器实例直接带上，不再白撞一次
        list(adapter.chat(messages=seeded))
        assert any(item["type"] == "reasoning" for item in bodies[2]["input"])


def test_reasoning_items_stay_absent_for_services_that_never_ask():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=_sse(_text("好"), _completed()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        list(_adapter(client).chat(messages=[
            *_messages(),
            ChatMessage(role="assistant", content="", tool_calls=(ToolCallRequest(id="seed", name="search_materials", arguments="{}"),)),
            ChatMessage(role="tool", content="[1] 片段", tool_call_id="seed"),
        ]))
    assert all(item["type"] != "reasoning" for body in bodies for item in body["input"])


# ---- 收尾原因与用量 ----

def test_usage_details_are_flattened_into_the_internal_names():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("好"), _completed(usage={
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 8},
            "output_tokens_details": {"reasoning_tokens": 3}})))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages()))[-1]

    assert isinstance(final, ChatFinal)
    assert final.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                           "cached_tokens": 8, "reasoning_tokens": 3}


def test_a_truncated_answer_maps_to_length():
    """Responses 用 status + incomplete_details 表达收尾；上报给客户端的说法与另一条协议对齐。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("先求外层，"), {
            "type": "response.incomplete",
            "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        }))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages()))[-1]

    assert isinstance(final, ChatFinal)
    assert final.finish_reason == "length"
    # 厂商原样说的是那个 reason，不归一化成别的说法。
    assert final.provider_finish_reason == "max_output_tokens"


def test_a_normal_finish_carries_the_provider_status():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("好"), _completed()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages()))[-1]

    assert isinstance(final, ChatFinal)
    assert final.finish_reason == "stop"
    assert final.provider_finish_reason == "completed"


def test_a_stream_without_a_terminal_event_leaves_the_provider_reason_null():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("好")))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        final = list(_adapter(client).chat(messages=_messages()))[-1]

    assert isinstance(final, ChatFinal)
    assert final.provider_finish_reason is None
    assert final.finish_reason == "stop"


def test_the_provider_reason_is_carried_out_of_a_tool_call_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "type": "function_call", "call_id": "c1", "name": "search_materials", "arguments": "{}"}},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        calls = list(_adapter(client).chat(messages=_messages(), tools=_TOOLS))[-1]

    assert isinstance(calls, ChatToolCalls)
    assert calls.provider_finish_reason == "completed"


def test_empty_answer_from_truncation_is_reported_as_such():
    """思考吃完输出预算时正文为空，报「空回答」会让人查错方向。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "response.reasoning_text.delta", "delta": "想了很久", "output_index": 0},
            {"type": "response.incomplete", "response": {
                "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}},
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))

    assert caught.value.code == "output_truncated"


def test_content_filtering_is_reported_as_such():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"type": "response.incomplete", "response": {
            "status": "incomplete", "incomplete_details": {"reason": "content_filter"}}}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))

    assert caught.value.code == "content_filtered"


def test_a_pointless_reasoning_retry_is_not_attempted():
    """没有可回传的思考内容时，补一条空条目改变不了请求体，别白打一次。
    reasoning.effort 配错值时服务端的说明里也有「reasoning」，正是这条路。"""
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "reasoning.effort: unknown variant `xx`"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))

    assert caught.value.code == "http_400"
    assert len(calls) == 1


def test_empty_answer_is_reported_as_invalid_response():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_completed()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as caught:
            list(adapter.chat(messages=_messages()))
    assert caught.value.code == "invalid_response"
    assert adapter.health()["last_error_code"] == "invalid_response"


# ---- 错误 ----

def test_a_failed_response_event_becomes_a_provider_error():
    """HTTP 200 之后厂商仍可能报失败，那条 error 说明要带出来。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_text("先"), {
            "type": "response.failed",
            "response": {"status": "failed", "error": {"code": "server_error", "message": "upstream exploded"}},
        }))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        stream = adapter.chat(messages=_messages())
        assert next(stream) == ChatDelta("先")
        with pytest.raises(LLMProviderError) as caught:
            list(stream)
    assert caught.value.code == "provider_failed"
    assert "upstream exploded" in str(caught.value)
    assert adapter.health()["last_error_code"] == "provider_failed"


def test_a_failed_response_does_not_hand_out_a_half_written_answer():
    """失败轮的半截正文不该当成结果下发。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse({"type": "response.failed", "response": {
            "status": "failed", "error": {"message": "boom"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "写了一半"}]}]}}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items: list[object] = []
        with pytest.raises(LLMProviderError):
            for item in _adapter(client).chat(messages=_messages()):
                items.append(item)
    assert items == []


def test_a_later_terminal_event_does_not_erase_the_error_that_arrived_first():
    """先来的 error 说的才是原因；收尾那份 error 是空的，不能把它盖掉。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "error", "code": "rate_limit_exceeded", "message": "slow down"},
            _completed(),
        ))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))
    assert "slow down" in str(caught.value)


def test_a_top_level_error_event_becomes_a_provider_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(
            {"type": "error", "code": "rate_limit_exceeded", "message": "slow down"}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))
    assert caught.value.code == "provider_failed"
    assert "slow down" in str(caught.value)


def test_provider_error_is_sanitized_and_recorded():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error": {"message": "Invalid api key: test-secret"},
            "debug": "sensitive-provider-body",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as caught:
            list(adapter.chat(messages=_messages()))
    assert caught.value.code == "http_401"
    assert caught.value.retryable is False
    assert "test-secret" not in str(caught.value)
    assert "***" in str(caught.value)
    assert "sensitive-provider-body" not in str(caught.value)
    assert adapter.health()["last_error_code"] == "http_401"


def test_error_detail_recognizes_top_level_shapes():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "unknown field: foo"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMProviderError) as caught:
            list(_adapter(client).chat(messages=_messages()))
    assert "unknown field: foo" in str(caught.value)


def test_retries_before_first_delta_then_succeeds():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, content=_sse(_text("答案"), _completed()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client, max_retries=1).chat(messages=_messages()))
    assert len(calls) == 2
    assert isinstance(items[-1], ChatFinal) and items[-1].text == "答案"


def test_mid_stream_drop_is_reported_as_stream_interrupted_not_retried():
    calls: list[int] = []

    def dropping_stream() -> Iterator[bytes]:
        yield b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"\xe9\x93\xbe\xe5\xbc\x8f"}\n\n'
        raise httpx.ReadError("connection lost")

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=dropping_stream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client, max_retries=2)
        stream = adapter.chat(messages=_messages())
        assert next(stream) == ChatDelta("链式")
        with pytest.raises(LLMProviderError) as caught:
            list(stream)
        assert caught.value.code == "stream_interrupted"
        assert len(calls) == 1  # 已输出 delta 后不得重放整轮
        assert adapter.health()["last_error_code"] == "stream_interrupted"


# ---- extra_body ----

def test_extra_body_is_merged_and_cannot_break_the_protocol():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(_text("好"), _completed()))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = ResponsesApiChat(
            api_key="k", base_url="https://api.example.com", model="m", provider="vendor-x",
            extra_body={"reasoning": {"effort": "high"}, "store": None}, client=client,
        )
        final = list(adapter.chat(messages=_messages()))[-1]

    body = captured["body"]
    assert body["reasoning"] == {"effort": "high"}
    # extra_body 里置 null 表示移除该字段。
    assert "store" not in body
    assert isinstance(final, ChatFinal) and final.provider == "vendor-x"

    # 覆盖协议字段会让流式解析崩在运行时，构造期就拦掉。
    with pytest.raises(ValueError, match="input"):
        ResponsesApiChat(api_key="k", base_url="https://api.example.com", model="m",
                         extra_body={"input": [], "stream": False})


def test_instructions_is_refused_with_the_actual_reason():
    """拦它不是因为「协议字段」——它会被静默插成第一条 system 消息，顶在语言规则前面。"""
    with pytest.raises(ValueError) as caught:
        ResponsesApiChat(api_key="k", base_url="https://api.example.com", model="m",
                         extra_body={"instructions": "你是助手"})
    assert "system" in str(caught.value) and "分类器" in str(caught.value)


def test_the_event_line_names_the_type_when_the_payload_does_not():
    """事件类型在负载里也有一份；只发 SSE event 行的服务靠后者兜底。"""
    frames = ('event: response.output_text.delta\ndata: {"delta": "好"}\n\n'
              'event: response.completed\ndata: {"response": {"status": "completed"}}\n\n')

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=frames.encode("utf-8"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client).chat(messages=_messages()))

    assert [item.text for item in items if isinstance(item, ChatDelta)] == ["好"]
    assert isinstance(items[-1], ChatFinal)
