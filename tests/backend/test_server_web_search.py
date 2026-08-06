"""厂商端联网搜索（Responses 协议的 server-side web_search）。

事件形状按真机实测构造（DeepSeek deepseek-v4-flash /responses，2026-08-06）：
`response.web_search_call.*` 三个状态事件只带 item_id，做了什么与成没成只在
`response.output_item.done` 的 web_search_call 条目上；一次响应里能有十几次搜索，
它们可以并行、事件交错；输出里的 annotations 恒为空数组（厂商不给来源明细）。
"""
from __future__ import annotations

import json
import time

import httpx
import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from adapters.llm import OpenAICompatibleChat, ResponsesApiChat
from adapters.llm.responses_api import _server_calls, to_input
from app.bootstrap import build_shared_runtime
from app.main import create_app
from contracts.llm import (ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, ServerToolCall,
                           ToolCallRequest, ToolSpec)
from core.settings import ModelChoice, Settings, _read_models

_TOOLS = (
    ToolSpec(name="search_materials", description="检索课程教材",
             parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
)


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="Responses API 支持哪些模型？")]


def _sse(*events: object) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                   for event in events).encode("utf-8")


def _text(delta: str, item: str = "msg_1") -> dict[str, object]:
    return {"type": "response.output_text.delta", "delta": delta, "item_id": item, "output_index": 1}


def _completed(**response: object) -> dict[str, object]:
    return {"type": "response.completed", "response": {"status": "completed", "output": [], **response}}


def _search_item(call_id: str, *, queries: list[str] | None = None, url: str = "",
                 action: str = "search", status: str = "completed") -> dict[str, object]:
    """真机形状：查询词列表末尾挂着厂商自己的追踪串，网址片段里也是同一个。"""
    body: dict[str, object] = {"type": "search", "queries": [*(queries or []), f"ws_call_id={call_id}"]} \
        if action == "search" else {"type": action, "url": url}
    return {"type": "web_search_call", "id": call_id, "status": status, "action": body}


def _added(item: dict[str, object], index: int) -> dict[str, object]:
    started = {"type": item["type"], "id": item["id"], "status": "in_progress"}
    return {"type": "response.output_item.added", "item": started, "output_index": index}


def _done(item: dict[str, object], index: int) -> dict[str, object]:
    return {"type": "response.output_item.done", "item": item, "output_index": index}


def _status(call_id: str, phase: str, index: int = 2) -> dict[str, object]:
    return {"type": f"response.web_search_call.{phase}", "item_id": call_id, "output_index": index}


def _adapter(client: httpx.Client, *, server_search: bool | None = None) -> ResponsesApiChat:
    """server_search=None 表示压根不传这个参数——默认值本身就是这一批的安全兜底。"""
    options = {} if server_search is None else {"server_search": server_search}
    return ResponsesApiChat(api_key="test-secret", base_url="https://api.example.com", model="example-model",
                            max_output_tokens=2048, max_retries=0, client=client, **options)


def _run(events: tuple[dict[str, object], ...], *, server_search: bool | None = True,
         tools: tuple[ToolSpec, ...] = ()) -> tuple[list[object], dict[str, object]]:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse(*events))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client, server_search=server_search).chat(messages=_messages(), tools=tools))
    return items, captured


# ---- 请求形状：默认关是这一批的安全兜底 ----

@pytest.mark.parametrize("server_search", [None, False], ids=["不传这个参数", "显式关掉"])
def test_the_search_tool_is_absent_by_default(server_search):
    """默认关：不传这个参数时，请求体与加它之前逐字一致。
    判据落在默认值上——只测「显式关掉」的话，默认值被改成开也测不出来。"""
    _, captured = _run((_text("好"), _completed()), server_search=server_search, tools=_TOOLS)
    assert [tool["type"] for tool in captured["body"]["tools"]] == ["function"]

    _, bare = _run((_text("好"), _completed()), server_search=server_search)
    assert "tools" not in bare["body"]


def test_enabling_it_appends_the_search_tool_after_the_local_ones():
    _, captured = _run((_text("好"), _completed()), tools=_TOOLS)
    tools = captured["body"]["tools"]
    # 本地工具照常在册，模型自己选用哪个；厂商端那条追加在后面。
    assert [tool["type"] for tool in tools] == ["function", "web_search"]
    assert tools[0]["name"] == "search_materials"
    assert tools[-1] == {"type": "web_search"}


def test_the_search_tool_alone_still_reaches_the_wire():
    """一件本地工具都不发的轮次（工具预算用完）也要带上它，否则这一轮等于没开。"""
    _, captured = _run((_text("好"), _completed()))
    assert captured["body"]["tools"] == [{"type": "web_search"}]


# ---- 事件解析 ----

def test_a_search_is_reported_with_its_queries():
    item = _search_item("call_00", queries=["DeepSeek Responses API 支持模型", "responses api models"])
    items, _ = _run((_added(item, 2), _status("call_00", "in_progress"), _status("call_00", "searching"),
                     _status("call_00", "completed"), _done(item, 2), _text("仅 flash。"), _completed()))
    final = items[-1]
    assert isinstance(final, ChatFinal)
    assert final.text == "仅 flash。"
    served = final.server_calls
    assert len(served) == 1
    assert (served[0].id, served[0].kind, served[0].action, served[0].ok) == ("call_00", "web_search", "search", True)
    # 追踪串是厂商塞进查询词里的，不该出现在界面上。
    assert served[0].detail == "DeepSeek Responses API 支持模型、responses api models"
    assert "ws_call_id" not in served[0].detail


def test_a_failed_page_open_is_reported_as_failed():
    """收尾事件在失败的调用上照发，成没成只有条目上说得准。"""
    item = _search_item("call_01", action="open_page", status="failed",
                        url="https://api-docs.deepseek.com/guides/responses_api/#ws_call_id=call_01")
    items, _ = _run((_added(item, 2), _status("call_01", "completed"), _done(item, 2),
                     _text("查不到。"), _completed()))
    served = items[-1].server_calls
    assert len(served) == 1 and served[0].ok is False
    assert served[0].action == "open_page"
    assert served[0].detail == "https://api-docs.deepseek.com/guides/responses_api/"


def test_a_turn_without_server_search_carries_no_such_calls():
    items, _ = _run((_text("好"), _completed()), server_search=False)
    assert items[-1].server_calls == ()


def test_parallel_searches_stay_apart_and_keep_their_order():
    """两次搜索能同时在飞，事件交错到达；串槽会让两条记录合成一条。"""
    first = _search_item("call_00", queries=["甲"])
    second = _search_item("call_01", action="open_page", url="https://example.com/b#ws_call_id=call_01")
    items, _ = _run((_added(first, 2), _added(second, 3),
                     _status("call_00", "searching", 2), _status("call_01", "searching", 3),
                     _status("call_00", "completed", 2), _done(first, 2),
                     _status("call_01", "completed", 3), _done(second, 3),
                     _text("答案"), _completed()))
    served = items[-1].server_calls
    assert [call.id for call in served] == ["call_00", "call_01"]
    assert [call.detail for call in served] == ["甲", "https://example.com/b"]


def test_a_function_call_in_the_same_round_is_not_disturbed():
    """真机实测同一轮里两者都会出现：厂商端搜索 + 本地函数调用。"""
    search = _search_item("call_00", queries=["天气"])
    function = {"type": "function_call", "id": "item_9", "call_id": "call_03",
                "name": "search_materials", "arguments": '{"query": "链式法则"}', "status": "completed"}
    items, _ = _run((_added(search, 2), _status("call_00", "completed"), _done(search, 2),
                     {"type": "response.output_item.added", "item": {**function, "arguments": "", "status": "in_progress"}, "output_index": 4},
                     {"type": "response.function_call_arguments.delta", "delta": '{"query": "链式法则"}',
                      "output_index": 4, "item_id": "item_9"},
                     _done(function, 4), _completed()), tools=_TOOLS)
    outcome = items[-1]
    assert isinstance(outcome, ChatToolCalls)
    assert [(call.id, call.name, call.arguments) for call in outcome.calls] == \
        [("call_03", "search_materials", '{"query": "链式法则"}')]
    assert [(call.id, call.detail) for call in outcome.server_calls] == [("call_00", "天气")]


def test_status_events_alone_do_not_conjure_a_record():
    """流在条目事件之前断了：半截记录说不出它做了什么，宁可不报。"""
    items, _ = _run((_status("call_00", "in_progress"), _status("call_00", "searching"),
                     _status("call_00", "completed"), _text("答案"), _completed()))
    assert items[-1].server_calls == ()


def test_the_terminal_output_backfills_a_service_that_sends_no_item_events():
    item = _search_item("call_00", queries=["甲"])
    items, _ = _run((_text("答案"), _completed(output=[item])))
    assert [call.id for call in items[-1].server_calls] == ["call_00"]


def test_the_time_the_vendor_spent_searching_is_recorded():
    """厂商端一次搜索能跑几十秒，耗时是这一步唯一的成本线索。
    直接驱动累加器：把开始时间往前挪 34.5 秒，等价于搜了这么久。"""
    searches: dict[str, dict[str, object]] = {}
    ResponsesApiChat._mark_search(searches, "call_00", done=False)
    searches["call_00"]["started"] = float(searches["call_00"]["started"]) - 34.5
    ResponsesApiChat._mark_search(searches, "call_00", done=True)
    ResponsesApiChat._absorb_search(searches, _search_item("call_00", queries=["甲"]), final=True)
    assert 34_400 <= _server_calls(searches)[0].duration_ms <= 34_600


# ---- 回传：不发回去，厂商就恢复不了自己那边的搜索结果 ----

def _echoed(reasoning: str = "想了想") -> list[dict]:
    raw = _search_item("call_00", queries=["甲"])
    served = ServerToolCall(id="call_00", kind="web_search", action="search", detail="甲", echo=raw)
    return to_input([
        ChatMessage(role="user", content="问"),
        ChatMessage(role="assistant", content="答", reasoning=reasoning, server_calls=(served,),
                    tool_calls=(ToolCallRequest(id="c1", name="search_materials", arguments="{}"),)),
        ChatMessage(role="tool", content="证据", tool_call_id="c1"),
    ])


def test_server_calls_are_echoed_verbatim_behind_the_reasoning():
    """真机实测的顺序约束：摆到思考内容之前，服务端会当成这一轮没回传思考而拒收（400）；
    摆到 function_call 与它的结果之间，则配不上对。"""
    items = _echoed()
    assert [item["type"] for item in items] == \
        ["message", "reasoning", "web_search_call", "message", "function_call", "function_call_output"]
    # 原样回传：厂商按自己塞进去的追踪串恢复结果，我们改一个字都可能让它对不上。
    assert items[2] == _search_item("call_00", queries=["甲"])


def test_a_plain_assistant_turn_echoes_them_too():
    """补救轮的形状：assistant 只有正文，没有工具调用也没有思考内容。
    真机实测服务端收（400 只发生在带 function_call 却缺思考内容的那种轮次）。"""
    raw = _search_item("call_00", queries=["甲"])
    served = ServerToolCall(id="call_00", kind="web_search", action="search", detail="甲", echo=raw)
    items = to_input([
        ChatMessage(role="user", content="问"),
        ChatMessage(role="assistant", content="答", server_calls=(served,)),
        ChatMessage(role="user", content="补一句提醒"),
    ])
    assert [item["type"] for item in items] == ["message", "web_search_call", "message", "message"]
    assert items[1] == raw


def test_an_echo_free_record_contributes_nothing():
    served = ServerToolCall(id="call_00", kind="web_search", action="search", detail="甲")
    assert to_input([ChatMessage(role="assistant", content="答", reasoning="想", server_calls=(served,),
                                 tool_calls=(ToolCallRequest(id="c1", name="t", arguments="{}"),))] ) == [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "想"}]},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "答"}]},
        {"type": "function_call", "call_id": "c1", "name": "t", "arguments": "{}"},
    ]


# ---- 配置 ----

def _env(**values: str):
    return lambda name, default="": values.get(name, default)


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        data_dir=tmp_path, database_path=tmp_path / "db.sqlite", uploads_dir=tmp_path / "materials",
        text_provider="vendor", text_base_url="https://api.example.com", text_api_key="k",
        text_model="m", enable_remote_llm=True, chunk_size=600, chunk_overlap=120, top_k_results=6,
        rag_embedding_model="", rag_reranker_model="", **overrides,
    )


def test_server_search_is_off_unless_asked_for():
    assert _read_models(_env(TEXT_MODEL="m"))[0].server_search is False
    assert _read_models(_env(TEXT_MODEL="m", TEXT_SERVER_SEARCH="0"))[0].server_search is False


def test_later_slots_inherit_the_switch_and_can_turn_it_off():
    models = _read_models(_env(TEXT_MODEL="m", TEXT_SERVER_SEARCH="1", TEXT_MODEL_2="m2",
                               TEXT_MODEL_3="m3", TEXT_SERVER_SEARCH_3="0"))
    assert [choice.server_search for choice in models] == [True, True, False]


def test_the_single_model_fallback_carries_the_switch(tmp_path):
    assert _settings(tmp_path).models[0].server_search is False
    assert _settings(tmp_path, text_server_search=True).models[0].server_search is True


def test_the_switch_reaches_the_responses_adapter(tmp_path):
    runtime = build_shared_runtime(_settings(tmp_path, text_protocol="responses", text_server_search=True))
    assert all(responder._server_search for responder in runtime.responders.values())
    # 分类器只从清单里挑一个 id，给它开联网是白花钱。
    assert runtime.classifier._server_search is False


def test_the_switch_is_ignored_and_reported_on_the_chat_protocol(tmp_path, caplog):
    """chat 协议上没有这个能力。静默忽略的话，用户会以为搜索已经开着。"""
    with caplog.at_level("WARNING"):
        runtime = build_shared_runtime(_settings(tmp_path, text_server_search=True))
    assert any("TEXT_SERVER_SEARCH" in record.getMessage() for record in caplog.records)
    assert all(isinstance(responder, OpenAICompatibleChat) for responder in runtime.responders.values())


def test_mixing_protocols_only_opens_the_search_on_the_responses_slot(tmp_path):
    settings = _settings(tmp_path, text_models=(
        ModelChoice(key="1", label="模型一", provider="v", base_url="https://api.example.com",
                    api_key="k", model="m", server_search=True),
        ModelChoice(key="2", label="模型二", provider="v", base_url="https://api.example.com",
                    api_key="k", model="m2", protocol="responses", server_search=True),
    ))
    runtime = build_shared_runtime(settings)
    assert isinstance(runtime.responders[("1", "high")], OpenAICompatibleChat)
    assert runtime.responders[("2", "high")]._server_search is True


# ---- 服务接线：厂商端做了什么要看得见 ----

class Scripted:
    mode, provider, model = "provider", "example", "example-model"

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def chat(self, *, messages, tools=()):
        self.calls.append({"messages": list(messages), "tools": tuple(tools)})
        yield from self._script.pop(0)

    def health(self):
        return {}

    def close(self):
        return None


def _service_settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="https://api.example.com/v1", text_api_key="",
        text_model="example-model", enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_service_settings(tmp_path))) as test_client:
        yield test_client


def _events(body: str) -> list[tuple[str, dict]]:
    frames = [frame for frame in body.split("\n\n") if frame]
    return [(frame.splitlines()[0].removeprefix("event: "),
             json.loads(frame.splitlines()[1].removeprefix("data: "))) for frame in frames]


def _served(call_id: str = "call_00", *, ok: bool = True) -> ServerToolCall:
    return ServerToolCall(id=call_id, kind="web_search", action="search", detail="链式法则 出处",
                          ok=ok, duration_ms=34500, echo=_search_item(call_id, queries=["链式法则 出处"]))


def _general_session(client: TestClient) -> str:
    """认不出课程的那条路：一件本地工具都不发，模型只有厂商端搜索可用。"""
    return client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()["id"]


def _course_session(client: TestClient) -> str:
    """课程模式：走带工具循环的主路。"""
    course = client.post("/api/v2/courses", json={"name": "微积分"}).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": ("notes.md", "链式法则：先对外层求导，再乘内层导数。", "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and \
            client.get(f"/api/v2/jobs/{job}").json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    return client.post("/api/v2/sessions",
                       json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]


def test_a_vendor_side_search_shows_up_as_an_activity_and_in_the_trace(client):
    """本地没有执行回环，这一步不上屏用户就只看到一段凭空出现的联网结论。"""
    session_id = _general_session(client)
    scripted = Scripted([[ChatDelta("仅 flash。"),
                          ChatFinal("仅 flash。", "stop", "example", "example-model", "provider",
                                    server_calls=(_served(),))]])
    workspace(client).turns._responder = scripted

    body = client.post(f"/api/v2/sessions/{session_id}/turns",
                       json={"client_request_id": "srv-1", "message": "Responses API 支持哪些模型？"}).text
    events = _events(body)
    call = next(payload for name, payload in events if name == "tool_call" and payload["call_id"] == "call_00")
    result = next(payload for name, payload in events if name == "tool_result" and payload["call_id"] == "call_00")
    # origin 是唯一分得开「我们执行的」与「厂商执行的」的字段。
    assert call["origin"] == "provider" and call["name"] == "web_search"
    assert result["ok"] is True and "厂商端检索" in result["summary"] and "链式法则 出处" in result["summary"]

    message = next(payload for name, payload in events if name == "turn_completed")
    stored = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"]
    assistant = next(item for item in stored if item["id"] == message["message_id"])
    entry = next(item for item in assistant["activity"] if item["call_id"] == "call_00")
    assert entry["origin"] == "provider" and entry["ok"] is True

    trace = client.get(f"/api/v2/sessions/{session_id}/trace").json()["turns"][-1]
    step = next(item for item in trace["tools"] if item["call_id"] == "call_00")
    assert step["origin"] == "provider" and step["duration_ms"] == 34500
    assert step["arguments"] == {"action": "search", "detail": "链式法则 出处"}


def test_a_failed_vendor_call_is_shown_as_failed(client):
    session_id = _general_session(client)
    workspace(client).turns._responder = Scripted([[
        ChatDelta("查不到。"),
        ChatFinal("查不到。", "stop", "example", "example-model", "provider",
                  server_calls=(_served(ok=False),))]])
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "srv-2", "message": "查一下"}).text)
    result = next(payload for name, payload in events if name == "tool_result")
    assert result["ok"] is False


def test_the_vendor_calls_ride_along_into_the_next_round(client):
    """带工具循环的主路：下一轮请求不带上它们，厂商就恢复不了搜索结果，这一轮的搜索白花。"""
    session_id = _course_session(client)
    scripted = Scripted([
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "链式法则"}'),),
                       server_calls=(_served(),))],
        [ChatDelta("先外层后内层。"),
         ChatFinal("先外层后内层。", "stop", "example", "example-model", "provider")],
    ])
    workspace(client).turns._responder = scripted
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "srv-3", "message": "链式法则怎么用？"}).text)

    followup = scripted.calls[-1]["messages"]
    # 第一条带 tool_calls 的 assistant 消息是种子检索那条合成消息，要找模型自己那一轮。
    assistant = next(item for item in followup
                     if item.role == "assistant" and any(call.id == "c1" for call in item.tool_calls))
    assert [call.id for call in assistant.server_calls] == ["call_00"]
    # 主路同样要报这一步：它和通用路是两段独立的循环。
    assert [payload["origin"] for name, payload in events
            if name == "tool_call" and payload["call_id"] == "call_00"] == ["provider"]


class Fleet:
    """父轮与子任务共用一个 responder，按系统提示里有没有子任务那段区分（照 test_delegate 的写法）。"""

    mode, provider, model = "provider", "example", "example-model"
    SUB_MARK = "你是一个子任务执行者"

    def __init__(self, parent, sub):
        self._parent, self._sub = list(parent), list(sub)
        self.sub_calls: list[list] = []

    def chat(self, *, messages, tools=()):
        is_sub = any(self.SUB_MARK in item.content for item in messages if item.role == "system")
        if is_sub:
            self.sub_calls.append(list(messages))
            yield from self._sub.pop(0) if self._sub else [
                ChatFinal("子任务收工。", "stop", "example", "m", "provider")]
            return
        yield from self._parent.pop(0) if self._parent else [
            ChatFinal("好的。", "stop", "example", "m", "provider")]

    def health(self):
        return {}

    def close(self):
        return None


def test_a_vendor_search_inside_a_subtask_is_reported_and_echoed(client):
    """子任务是另一段循环，那里发不出 SSE。不接出来的话，子任务联网查到的东西
    既不上屏也不回传——回答里凭空多出一段网络结论。"""
    session_id = _course_session(client)
    delegate_args = json.dumps({"task": "查清这几种算法的取舍", "expect": "对比结论加出处"}, ensure_ascii=False)
    fleet = Fleet(
        parent=[[ChatToolCalls((ToolCallRequest("d1", "delegate", delegate_args),))],
                [ChatDelta("综合下来 SJF 更短。"),
                 ChatFinal("综合下来 SJF 更短。", "stop", "example", "m", "provider")]],
        sub=[[ChatToolCalls((ToolCallRequest("s0", "search_materials", '{"query": "链式法则"}'),),
                            server_calls=(_served("call_09"),))],
             [ChatFinal("子任务查到：先外层后内层。", "stop", "example", "m", "provider")]],
    )
    workspace(client).turns._responder = fleet
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "srv-5",
                                       "message": "帮我深入研究一下这门课里几种调度算法的取舍"}).text)

    # 上屏：父子两边的 id 都由厂商生成，子任务那条挂 sub: 前缀免得撞号。
    call = next(payload for name, payload in events
                if name == "tool_call" and payload["call_id"] == "sub:call_09")
    assert call["origin"] == "provider" and call["name"] == "web_search"
    assert any(name == "tool_result" and payload["call_id"] == "sub:call_09" for name, payload in events)

    message = next(payload for name, payload in events if name == "turn_completed")
    stored = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"]
    assistant = next(item for item in stored if item["id"] == message["message_id"])
    assert any(entry["call_id"] == "sub:call_09" for entry in assistant["activity"])

    # 回传：子循环下一轮不带上它，厂商就恢复不了搜索结果。
    followup = fleet.sub_calls[-1]
    echoed = next(item for item in followup if item.role == "assistant" and item.tool_calls)
    assert [item.id for item in echoed.server_calls] == ["call_09"]


def test_a_turn_without_vendor_calls_adds_no_chip(client):
    """开关关着时（默认），这条链路一个事件都不该多出来。"""
    session_id = _general_session(client)
    workspace(client).turns._responder = Scripted([[
        ChatDelta("好。"), ChatFinal("好。", "stop", "example", "example-model", "provider")]])
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "srv-4", "message": "你好"}).text)
    assert not [payload for name, payload in events
                if name in {"tool_call", "tool_result"} and payload.get("origin") == "provider"]
