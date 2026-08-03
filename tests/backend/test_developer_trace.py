"""开发者模式的 trace 回读：按会话取全部轮次，把点中的那一轮标出来。

trace 是旁路观测设施——写失败只打 warning，payload 可以被单独清理，整个目录也可以删。
所以这条链路的每种缺失都要有对应的空态，别报 500 也别静默给个空数组。

时序、耗时、参数与 usage 来自 trace；工具取回的正文来自 messages 表 role='tool' 的行。
两边在这里合成一份，别的地方不该再拼第二套。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.http.devtools import (MAX_DAY_FILES, MAX_PAYLOAD_BYTES, MAX_PAYLOAD_TOTAL_BYTES,
                               MAX_SCAN_LINES, MAX_TURNS)
from app.main import create_app
from contracts.llm import (ChatDelta, ChatFinal, ChatReasoning, ChatToolCalls, LLMProviderError,
                           ToolCallRequest)
from core.settings import Settings
from modules.agent.context import SEED_CALL_ID

MATERIAL_TEXT = "FIFO 调度下长作业会拖住后面的短作业，这就是护航效应。SJF 优先跑最短的作业。"
ALICE = {"X-CoursePilot-User": "alice"}
BOB = {"X-CoursePilot-User": "bob"}


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


class Scripted:
    mode, provider, model = "provider", "example", "example-model"

    def __init__(self, script):
        self._script = list(script)

    def chat(self, *, messages, tools=()):
        yield from self._script.pop(0) if self._script else [
            ChatFinal("好的。", "stop", "example", "m", "provider")]

    def health(self):
        return {}

    def close(self):
        return None


def _indexed_course(client, *, name="操作系统", filename="os.md", text=MATERIAL_TEXT, headers=None) -> str:
    course = client.post("/api/v2/courses", json={"name": name}, headers=headers).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": (filename, text, "text/markdown")}, headers=headers).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index", headers=headers).json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/v2/jobs/{job}", headers=headers).json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    return course["id"]


def _session(client, course_id, headers=None) -> str:
    return client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id},
                       headers=headers).json()["id"]


def _turn(client, session_id, *, request_id, message, reply="因为长作业挡在前面。[1]", headers=None) -> None:
    app = client.app.state.workspaces.for_username(headers[list(headers)[0]]) if headers else workspace(client)
    app.turns._responder = Scripted([[ChatDelta(reply), ChatFinal(reply, "stop", "example", "m", "provider")]])
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": request_id, "message": message}, headers=headers)


def _space(client, headers=None):
    if headers:
        return client.app.state.workspaces.for_username(headers[list(headers)[0]])
    return workspace(client)


def _trace_dir(client, headers=None) -> Path:
    return _space(client, headers).settings.data_dir / "traces"


def _write_records(directory: Path, day: str, records: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{day}.jsonl").open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _record(*, session_id: str, turn_id: str, started_at: str, **extra) -> dict:
    return {"kind": "turn", "session_id": session_id, "turn_id": turn_id, "started_at": started_at,
            "status": "completed", "scope_mode": "course", "prompt_version": "tutor_v19",
            "duration_ms": 1200, "tools": [], **extra}


def _fetch(client, session_id, *, turn_id=None, headers=None):
    query = f"?turn_id={turn_id}" if turn_id else ""
    return client.get(f"/api/v2/sessions/{session_id}/trace{query}", headers=headers)


def _turn_ids(payload) -> list[str]:
    return [item["turn_id"] for item in payload["turns"]]


def _body_text(client, session_id, turn_id, call_id, headers=None) -> str | None:
    """正文不随列表下发，点开哪一步才取哪一条。"""
    return client.get(f"/api/v2/sessions/{session_id}/trace/body",
                      params={"turn_id": turn_id, "call_id": call_id}, headers=headers).json()["text"]


# ---- 正常路径：trace 给时序，messages 给正文 ----

def test_a_completed_turn_carries_both_the_trace_span_and_the_tool_body(client):
    """一次检索之后，侧栏要同时拿到：种子检索这一步的耗时与参数（trace），
    以及它取回的教材原文（messages 表 role='tool'）。缺哪一半这个功能都没做完。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")

    payload = _fetch(client, session_id).json()
    assert payload["turns"], "trace 里一条都没有"
    view = payload["turns"][-1]
    assert view["trace_record"] is True
    assert payload["focus_turn_id"] == view["turn_id"] and payload["focus_found"] is True
    assert view["status"] == "completed" and isinstance(view["duration_ms"], int)
    assert view["responder"] and view["usage"] is not None

    seed = next(tool for tool in view["tools"] if tool["origin"] == "seed")
    assert seed["name"] == "search_materials"
    assert seed["arguments"] == {"query": "FIFO 为什么有护航效应"}
    assert seed["body"] is not None, "trace 有这一步，messages 里的正文没接上来"
    assert seed["body_state"] == "stored"
    text = _body_text(client, session_id, view["turn_id"], seed["body"]["call_id"])
    assert "护航效应" in text and "os.md" in text
    assert seed["body"]["chars"] == len(text)


def test_without_a_turn_id_the_newest_turn_is_the_one_in_focus(client):
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    _turn(client, session_id, request_id="t-2", message="那 SJF 呢")

    payload = _fetch(client, session_id).json()
    assert len(payload["turns"]) == 2
    assert payload["focus_turn_id"] == payload["turns"][-1]["turn_id"]


def test_an_earlier_turn_can_be_put_in_focus_without_losing_the_others(client):
    """两种口径同一个接口：整个会话都在 turns 里，点中的那一轮由 focus_turn_id 指出来。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    _turn(client, session_id, request_id="t-2", message="那 SJF 呢")

    first = _fetch(client, session_id).json()["turns"][0]["turn_id"]
    payload = _fetch(client, session_id, turn_id=first).json()
    assert payload["focus_turn_id"] == first and payload["focus_found"] is True
    assert len(payload["turns"]) == 2, "换个高亮对象把别的轮次弄丢了"


def test_turns_come_back_oldest_first(client):
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    for index in range(3):
        _turn(client, session_id, request_id=f"t-{index}", message=f"第 {index} 问：护航效应")
    payload = _fetch(client, session_id).json()
    stamps = [item["started_at"] for item in payload["turns"]]
    assert stamps == sorted(stamps)


# ---- 两种空态 ----

def test_a_turn_with_no_trace_record_says_so_instead_of_failing(client):
    """trace 目录可以被整个清掉。这时消息还在，接口要如实报「这一轮没有记录」。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    turn_id = _fetch(client, session_id).json()["turns"][-1]["turn_id"]

    directory = _trace_dir(client)
    for path in directory.glob("*.jsonl"):
        path.unlink()

    response = _fetch(client, session_id, turn_id=turn_id)
    assert response.status_code == 200
    payload = response.json()
    assert payload["focus_found"] is False
    # 正文还在库里，所以那一轮仍然值得画出来，只是标明没有 trace
    view = next(item for item in payload["turns"] if item["turn_id"] == turn_id)
    assert view["trace_record"] is False
    assert view["started_at"] is None and view["tools"] == []
    assert view["unmatched_bodies"], "trace 没了，库里还留着的正文也要列出来"
    texts = [_body_text(client, session_id, turn_id, body["call_id"]) for body in view["unmatched_bodies"]]
    assert any(text and "护航效应" in text for text in texts)


def test_a_session_with_neither_trace_nor_bodies_comes_back_empty_not_broken(client):
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    response = _fetch(client, session_id)
    assert response.status_code == 200
    payload = response.json()
    assert payload["turns"] == [] and payload["focus_turn_id"] is None and payload["focus_found"] is False


def test_a_cleaned_payload_leaves_the_reference_and_its_size_visible(client):
    """长参数搬进 payloads/ 之后可以单独删。索引里只剩 ref 时不能装作参数是空的。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="护航效应" * 80)

    directory = _trace_dir(client)
    payloads = list((directory / "payloads").glob("*.json"))
    assert payloads, "前置条件不成立：这一轮没有搬出 payload，测不到缺失分支"
    resolved = _fetch(client, session_id).json()["turns"][-1]
    assert resolved["payload_state"] == "resolved"
    assert isinstance(resolved["tools"][0]["arguments"], dict) and "query" in resolved["tools"][0]["arguments"]

    for path in payloads:
        path.unlink()

    view = _fetch(client, session_id).json()["turns"][-1]
    assert view["payload_state"] == "missing"
    stub = view["tools"][0]
    assert stub["arguments"] is None
    assert stub["arguments_ref"] and stub["arguments_ref"]["chars"] > 200


# ---- 跨天 ----

def test_a_session_that_crosses_midnight_is_stitched_from_both_day_files(client):
    """一个会话跨零点就分在两个日期文件里。少读一个文件，用户看到的历史就断一半。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")

    directory = _trace_dir(client)
    real = _fetch(client, session_id).json()["turns"][-1]
    day = str(real["started_at"])[:10]
    _write_records(directory, day, [_record(session_id=session_id, turn_id="late", started_at=f"{day}T23:59:30Z")])
    tomorrow = f"{day[:8]}{int(day[8:10]) + 1:02d}"
    _write_records(directory, tomorrow, [_record(session_id=session_id, turn_id="early", started_at=f"{tomorrow}T00:00:20Z")])

    payload = _fetch(client, session_id, turn_id="early").json()
    ids = _turn_ids(payload)
    assert "late" in ids and "early" in ids, f"跨天没拼起来：{ids}"
    assert ids.index("late") < ids.index("early")
    assert len([name for name in payload["scan"]["files"] if name.endswith(".jsonl")]) >= 2


def test_day_files_outside_the_session_window_are_not_even_opened(client):
    """按会话的消息时间窗挑日期文件。不挑的话，一个上月的会话要把这个月每天的文件都翻一遍。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")

    directory = _trace_dir(client)
    _write_records(directory, "2019-01-01", [_record(session_id=session_id, turn_id="ancient", started_at="2019-01-01T00:00:00Z")])

    payload = _fetch(client, session_id).json()
    assert "2019-01-01.jsonl" not in payload["scan"]["files"]
    assert "ancient" not in _turn_ids(payload)


# ---- 扫描上限 ----

def test_a_huge_day_file_is_streamed_and_the_scan_stops_at_the_limit(client, monkeypatch):
    """一整天的 trace 可以有几万行。判据两条：不许整份读进内存，扫描行数有上限并如实上报。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]

    noise = [_record(session_id="other-session", turn_id=f"n-{index}", started_at=f"{day}T01:00:00Z")
             for index in range(MAX_SCAN_LINES + 5_000)]
    _write_records(directory, day, noise)

    original = Path.read_text

    def guarded(self, *args, **kwargs):
        assert self.suffix != ".jsonl", f"整份读进内存了：{self.name}"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)

    payload = _fetch(client, session_id).json()
    assert payload["scan"]["scanned_lines"] <= MAX_SCAN_LINES
    assert payload["scan"]["scan_capped"] is True and payload["scan"]["truncated"] is True
    assert payload["limits"]["max_scan_lines"] == MAX_SCAN_LINES


def test_the_number_of_returned_turns_is_capped(client):
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]

    _write_records(directory, day, [
        _record(session_id=session_id, turn_id=f"bulk-{index:04d}", started_at=f"{day}T02:{index % 60:02d}:00Z")
        for index in range(MAX_TURNS * 2)])

    payload = _fetch(client, session_id).json()
    assert len(payload["turns"]) <= MAX_TURNS
    assert payload["scan"]["turns_capped"] is True and payload["scan"]["truncated"] is True
    assert payload["limits"]["max_turns"] == MAX_TURNS


def test_the_focused_turn_survives_the_turn_cap(client):
    """点中的那一轮可能是很旧的一条。上限按新的留，会正好把用户点的那条挤掉。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]

    _write_records(directory, day, [
        _record(session_id=session_id, turn_id=f"bulk-{index:04d}", started_at=f"{day}T03:{index % 60:02d}:{index % 60:02d}Z")
        for index in range(MAX_TURNS * 2)])

    payload = _fetch(client, session_id, turn_id="bulk-0000").json()
    assert payload["focus_found"] is True, "上限把用户点的那一轮挤掉了"
    assert "bulk-0000" in _turn_ids(payload)
    assert len(payload["turns"]) <= MAX_TURNS
    # 补回来的那一条要落在它自己的时间位置上，不能吊在列表末尾
    stamps = [item["started_at"] for item in payload["turns"]]
    assert stamps == sorted(stamps), f"补回 focus 之后顺序乱了：{stamps[:5]}"


def test_the_number_of_day_files_is_capped_and_says_so(client):
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    year, month = int(day[:4]), int(day[5:7])

    # 会话窗口只覆盖今天，所以这些文件要用「日期不可判」的名字才会全部进入候选
    for index in range(MAX_DAY_FILES + 5):
        _write_records(directory, f"partial-{index:03d}", [
            _record(session_id=session_id, turn_id=f"f-{index}", started_at=f"{year:04d}-{month:02d}-01T00:00:00Z")])

    payload = _fetch(client, session_id).json()
    assert len(payload["scan"]["files"]) <= MAX_DAY_FILES
    assert payload["scan"]["files_capped"] is True and payload["scan"]["truncated"] is True


# ---- 不串 ----

def test_another_session_in_the_same_day_file_never_leaks_in(client):
    """同一个日期文件里挨着的就是别的会话。history_read 有过跨课程取证的先例，这里必须挡住。"""
    course_id = _indexed_course(client)
    mine = _session(client, course_id)
    other = _session(client, course_id)
    _turn(client, mine, request_id="a-1", message="FIFO 为什么有护航效应")
    _turn(client, other, request_id="b-1", message="那 SJF 呢")

    payload = _fetch(client, mine).json()
    assert len(payload["turns"]) == 1
    other_ids = {item["turn_id"] for item in _fetch(client, other).json()["turns"]}
    assert other_ids.isdisjoint(set(_turn_ids(payload)))
    for view in payload["turns"]:
        texts = [_body_text(client, mine, view["turn_id"], body["call_id"])
                 for body in view["unmatched_bodies"]]
        assert all("SJF" not in (text or "") for text in texts)


def test_a_line_that_merely_mentions_the_session_id_is_still_another_session(client):
    """按行做的是子串预筛，省掉绝大多数 json.loads；判断归属只能看 session_id 字段。

    会话 id 会出现在别的会话的工具参数里（用户把它贴进提问、子任务把它写进调研笔记），
    这时那一行照样通过预筛。少了字段比对，别人的整轮 trace 就跟着进来了。
    """
    course_id = _indexed_course(client)
    mine = _session(client, course_id)
    other = _session(client, course_id)
    _turn(client, mine, request_id="a-1", message="FIFO 为什么有护航效应")

    directory = _trace_dir(client)
    day = str(_fetch(client, mine).json()["turns"][-1]["started_at"])[:10]
    _write_records(directory, day, [_record(
        session_id=other, turn_id="not-mine", started_at=f"{day}T07:00:00Z",
        tools=[{"origin": "model", "name": "search_materials", "ok": True,
                "arguments": {"query": f"帮我看看 {mine} 这个会话"}}])])

    payload = _fetch(client, mine).json()
    assert "not-mine" not in _turn_ids(payload), "只靠子串预筛判归属，别人的轮次漏进来了"


def test_another_users_trace_is_out_of_reach(client):
    """trace 按数据目录隔离。拿着别人的 session_id 问，只能是 404。"""
    alice_course = _indexed_course(client, name="甲的操作系统", headers=ALICE)
    bob_course = _indexed_course(client, name="乙的操作系统", headers=BOB)
    alice_session = _session(client, alice_course, headers=ALICE)
    bob_session = _session(client, bob_course, headers=BOB)
    _turn(client, alice_session, request_id="a-1", message="FIFO 为什么有护航效应", headers=ALICE)
    _turn(client, bob_session, request_id="b-1", message="FIFO 为什么有护航效应", headers=BOB)

    assert _trace_dir(client, ALICE) != _trace_dir(client, BOB)
    assert _fetch(client, alice_session, headers=BOB).status_code == 404
    assert _fetch(client, bob_session, headers=ALICE).status_code == 404

    alice_ids = set(_turn_ids(_fetch(client, alice_session, headers=ALICE).json()))
    bob_ids = set(_turn_ids(_fetch(client, bob_session, headers=BOB).json()))
    assert alice_ids and bob_ids and alice_ids.isdisjoint(bob_ids)


def test_an_unknown_session_is_a_404(client):
    assert _fetch(client, "session-does-not-exist").status_code == 404


# ---- 正文合并的边界 ----

def test_a_subagent_body_is_labelled_instead_of_glued_onto_a_parent_span(client):
    """子任务的正文用 sub: 前缀落库，父轮的 trace 里没有对应的 span。
    按顺序硬接会把子任务查到的东西挂到父轮某次调用底下。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    turn_id = _fetch(client, session_id).json()["turns"][-1]["turn_id"]

    space = _space(client)
    space.sessions.append_message(session_id=session_id, turn_id=turn_id, role="tool",
                                  content="子任务查到的网页正文", activity=[{"call_id": "sub:c9", "name": "web_search"}])

    view = _fetch(client, session_id).json()["turns"][-1]
    assert [body["call_id"] for body in view["subagent_bodies"]] == ["sub:c9"]
    attached = [_body_text(client, session_id, turn_id, tool["body"]["call_id"])
                for tool in view["tools"] if tool["body"]]
    assert all("子任务" not in (text or "") for text in attached)


def test_two_calls_of_the_same_tool_keep_their_own_text(client):
    """trace 的 span 不记 call_id，同名工具只能按顺序接。接串了，用户看到的就是
    「查 A 拿回了 B 的结果」。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "护航效应"}'),
                        ToolCallRequest("c2", "search_materials", '{"query": "轮转调度"}')))],
        [ChatDelta("查完了。"), ChatFinal("查完了。", "stop", "example", "m", "provider")],
    ]
    space = _space(client)
    space.turns._responder = Scripted(script)
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": "t-1", "message": "护航效应和轮转分别是什么"})

    view = _fetch(client, session_id).json()["turns"][-1]
    model_spans = [tool for tool in view["tools"] if tool["origin"] == "model"]
    assert len(model_spans) == 2, view["tools"]
    assert view["unmatched_bodies"] == [], "有正文没接上去"
    first, second = (tool["body"] for tool in model_spans)
    assert first is not None and second is not None
    assert first["call_id"] == "c1" and second["call_id"] == "c2", "两次调用的正文接反了"


def test_a_reused_call_has_no_text_and_does_not_steal_the_next_ones(client):
    """同一轮里参数相同的第二次调用直接复用，正文不会存第二份。
    按顺序接的时候它不能把后面那次的正文顶上来。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "护航效应"}'),
                        ToolCallRequest("c2", "search_materials", '{"query": "护航效应"}'),
                        ToolCallRequest("c3", "search_materials", '{"query": "轮转调度"}')))],
        [ChatDelta("查完了。"), ChatFinal("查完了。", "stop", "example", "m", "provider")],
    ]
    space = _space(client)
    space.turns._responder = Scripted(script)
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": "t-1", "message": "护航效应和轮转分别是什么"})

    view = _fetch(client, session_id).json()["turns"][-1]
    model_spans = [tool for tool in view["tools"] if tool["origin"] == "model"]
    assert [tool.get("reused") for tool in model_spans] == [None, True, None], model_spans
    assert [tool["body"] and tool["body"]["call_id"] for tool in model_spans] == ["c1", None, "c3"]
    assert view["unmatched_bodies"] == []


def test_many_long_bodies_cost_the_listing_almost_nothing(client):
    """一个会话几十轮检索，正文加起来能有几兆。列表只报每段多长，
    所以正文再多，打开侧栏的那一次响应也不会跟着涨。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    turn_id = _fetch(client, session_id).json()["turns"][-1]["turn_id"]
    lean = len(_fetch(client, session_id).content)

    space = _space(client)
    count = 25
    for index in range(count):
        space.sessions.append_message(session_id=session_id, turn_id=turn_id, role="tool",
                                      content="长" * 4_000, activity=[{"call_id": f"sub:x{index}", "name": "web_fetch"}])

    response = _fetch(client, session_id)
    view = response.json()["turns"][-1]
    assert len(view["subagent_bodies"]) == count
    assert all(body["chars"] == 4_000 for body in view["subagent_bodies"]), "连原本多长都没说"
    # 25 段 4000 字符的正文是 10 万字符；列表只多出这些段落的元数据
    assert len(response.content) - lean < 4_000, "正文跟着列表一起下来了"
    assert _body_text(client, session_id, turn_id, "sub:x0") == "长" * 4_000


def test_fields_the_reader_does_not_know_about_still_reach_the_response(client):
    """trace 的字段会随功能增加。读端按白名单挑就会静默丢掉新字段，所以其余的原样带上。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    _write_records(directory, day, [_record(session_id=session_id, turn_id="odd",
                                            started_at=f"{day}T04:00:00Z",
                                            plan_reminder=True, something_new={"a": 1})])

    view = next(item for item in _fetch(client, session_id).json()["turns"] if item["turn_id"] == "odd")
    assert view["extras"]["plan_reminder"] is True
    assert view["extras"]["something_new"] == {"a": 1}
    assert "session_id" not in view["extras"], "会话 id 不是本轮的观测数据，别塞进杂项里"


def test_a_corrupt_line_is_skipped_instead_of_breaking_the_whole_read(client):
    """JSONL 是追加写的，进程被杀会留下半行。一条坏行不该让整个侧栏打不开。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    with (directory / f"{day}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "turn", "session_id": "' + session_id + '", "turn_i\n')
    _write_records(directory, day, [_record(session_id=session_id, turn_id="after", started_at=f"{day}T05:00:00Z")])

    payload = _fetch(client, session_id).json()
    assert payload["turns"], "一条坏行把整份读挂了"
    assert "after" in _turn_ids(payload)


def test_a_single_oversized_payload_file_is_not_read(client):
    """单文件上限：payload 正常是几 KB，异常大的那份不该被整个读进内存。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    (directory / "payloads").mkdir(parents=True, exist_ok=True)
    (directory / "payloads" / "fat.json").write_text(
        json.dumps({"tools": {"0.arguments": {"query": "长" * MAX_PAYLOAD_BYTES}}}, ensure_ascii=False),
        encoding="utf-8")
    _write_records(directory, day, [_record(
        session_id=session_id, turn_id="fat", started_at=f"{day}T08:00:00Z",
        payload_ref="payloads/fat.json",
        tools=[{"origin": "model", "name": "search_materials", "ok": True,
                "arguments": {"payload_ref": "tools.0.arguments", "chars": 999}}])])

    view = next(item for item in _fetch(client, session_id).json()["turns"] if item["turn_id"] == "fat")
    assert view["payload_state"] == "oversized"
    assert view["tools"][0]["arguments"] is None


def test_the_payload_budget_is_shared_across_the_whole_response(client):
    """一轮的 payload 不大，五十轮加起来可以很大。总量用满之后剩下的标 skipped。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    (directory / "payloads").mkdir(parents=True, exist_ok=True)

    # 每份都在单文件上限之内，加起来超出总量：这样撞到的才是总闸，不是单文件那道
    chunk = MAX_PAYLOAD_BYTES // 2
    count = MAX_PAYLOAD_TOTAL_BYTES // chunk + 3
    for index in range(count):
        (directory / "payloads" / f"p{index}.json").write_text(
            json.dumps({"tools": {"0.arguments": {"query": "x" * chunk}}}), encoding="utf-8")
    _write_records(directory, day, [_record(
        session_id=session_id, turn_id=f"big-{index}", started_at=f"{day}T09:{index:02d}:00Z",
        payload_ref=f"payloads/p{index}.json",
        tools=[{"origin": "model", "name": "search_materials", "ok": True,
                "arguments": {"payload_ref": "tools.0.arguments", "chars": chunk}}])
        for index in range(count)])

    views = [item for item in _fetch(client, session_id).json()["turns"] if item["turn_id"].startswith("big-")]
    states = [item["payload_state"] for item in views]
    assert len(views) == count
    assert "skipped" in states, f"总量没有闸：{states}"
    assert sum(state == "resolved" for state in states) * chunk <= MAX_PAYLOAD_TOTAL_BYTES


def test_a_payload_ref_pointing_outside_the_payload_directory_is_refused(client):
    """ref 是磁盘上的字符串，拼路径前要挡住 ../ ——目录被别的进程写过就可能不是原样。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    _write_records(directory, day, [_record(
        session_id=session_id, turn_id="escapee", started_at=f"{day}T06:00:00Z",
        payload_ref="../../../etc/passwd",
        tools=[{"origin": "model", "name": "search_materials", "ok": True,
                "arguments": {"payload_ref": "tools.0.arguments", "chars": 900}}])])

    view = next(item for item in _fetch(client, session_id).json()["turns"] if item["turn_id"] == "escapee")
    assert view["payload_state"] == "invalid"
    assert view["tools"][0]["arguments"] is None


def test_the_endpoint_pairs_by_call_id_too_not_just_the_helper(client):
    """span 记了 call_id，但视图得把它带出来才用得上。带不出来时精确配对整条静默失效，
    退回按工具名先来先接——落库顺序和 span 顺序一错位就接反。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    turn_id = _fetch(client, session_id).json()["turns"][-1]["turn_id"]

    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    _write_records(directory, day, [_record(
        session_id=session_id, turn_id="pairing", started_at=f"{day}T11:00:00Z",
        tools=[{"call_id": "c1", "round": 1, "origin": "model", "name": "search_materials", "ok": True},
               {"call_id": "c2", "round": 1, "origin": "model", "name": "search_materials", "ok": True}])])

    # 落库顺序和 span 顺序相反：重试、并发写都会这样
    space = _space(client)
    for call_id in ("c2", "c1"):
        space.sessions.append_message(session_id=session_id, turn_id="pairing", role="tool",
                                      content=f"{call_id} 的正文", activity=[{"call_id": call_id, "name": "search_materials"}])

    view = next(item for item in _fetch(client, session_id).json()["turns"] if item["turn_id"] == "pairing")
    assert [tool["body"]["call_id"] for tool in view["tools"]] == ["c1", "c2"], "视图把 call_id 丢了，接反了"
    assert view["tools"][0]["call_id"] == "c1"


def test_bodies_pair_by_call_id_even_when_they_arrive_out_of_order():
    """按工具名先来先接只在「正文与 span 同序」时才对。span 现在记了 call_id，
    乱序也认得出来——同名工具连调几次时，接错一位就是「查 A 拿回了 B 的结果」。"""
    from app.http.devtools import _attach_bodies

    view = {
        "tools": [
            {"call_id": "c1", "name": "search_materials", "origin": "model", "body": None},
            {"call_id": "c2", "name": "search_materials", "origin": "model", "body": None},
            {"call_id": "c3", "name": "search_materials", "origin": "model", "body": None},
        ],
        "subagent_bodies": [],
    }
    # 落库顺序与 span 顺序不一致（重试、并发写都会这样）
    bodies = [{"call_id": "c3", "name": "search_materials", "chars": 3},
              {"call_id": "c1", "name": "search_materials", "chars": 1},
              {"call_id": "c2", "name": "search_materials", "chars": 2}]
    _attach_bodies(view, bodies)

    assert [tool["body"]["call_id"] for tool in view["tools"]] == ["c1", "c2", "c3"]
    assert view["unmatched_bodies"] == []


# ---- ReAct 执行流程：思考 → 调工具 → 又说了什么 → 最终回答 ----

def _react_turn(client, session_id, script, *, request_id="r-1", message="护航效应是怎么回事"):
    space = _space(client)
    space.turns._responder = Scripted(script)
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": request_id, "message": message})
    return _fetch(client, session_id).json()["turns"][-1]


def test_each_model_round_leaves_its_thinking_its_text_and_the_calls_it_made(client):
    """侧栏第一眼要看到的是这条链：第 1 轮想了什么、说了什么、调了哪几个工具，
    第 2 轮又是什么，最后给了什么答案。trace 里只有 answer_chars 时这条链重建不出来。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatReasoning("先看看教材里护航效应在哪一节。"), ChatDelta("我先查一下教材。"),
         ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "护航效应"}'),))],
        [ChatReasoning("材料够了，可以下结论。"), ChatDelta("再看看 SJF。"),
         ChatToolCalls((ToolCallRequest("c2", "search_materials", '{"query": "SJF"}'),))],
        [ChatDelta("长作业挡在前面。[1]"), ChatFinal("长作业挡在前面。[1]", "stop", "example", "m", "provider")],
    ]
    view = _react_turn(client, session_id, script)

    react = view["react"]
    steps = react["steps"]
    assert [step["round"] for step in steps] == [1, 2, 3], steps
    assert steps[0]["reasoning"] == "先看看教材里护航效应在哪一节。"
    assert steps[0]["text"] == "我先查一下教材。"
    assert steps[0]["calls"] == ["c1"] and steps[0]["outcome"] == "tool_calls"
    assert steps[1]["reasoning"] == "材料够了，可以下结论。"
    assert steps[1]["calls"] == ["c2"]
    assert steps[2]["calls"] == [] and steps[2]["outcome"] == "final"
    assert react["answer"] == "长作业挡在前面。[1]"
    assert react["answer_chars"] == len("长作业挡在前面。[1]")


def test_a_round_without_thinking_leaves_no_thinking_field(client):
    """不带思考的档位跑一轮，那一块要整个不出现，而不是显示成空——空看起来像出了错。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [[ChatDelta("长作业挡在前面。[1]"),
               ChatFinal("长作业挡在前面。[1]", "stop", "example", "m", "provider")]]
    view = _react_turn(client, session_id, script)

    step = view["react"]["steps"][0]
    assert step["reasoning"] is None and step["reasoning_chars"] == 0


def test_a_tool_span_says_which_round_issued_it(client):
    """时序要能把调用挂回它所属的那一轮：种子检索在模型开口之前，算第 0 轮。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "护航效应"}'),))],
        [ChatDelta("好了。"), ChatFinal("好了。", "stop", "example", "m", "provider")],
    ]
    view = _react_turn(client, session_id, script)

    rounds = {tool["call_id"]: tool["round"] for tool in view["tools"]}
    assert rounds[SEED_CALL_ID] == 0, rounds
    assert rounds["c1"] == 1, rounds


def test_a_server_injected_round_is_marked_as_such(client):
    """补救轮是服务端补的，不是模型自己要的。看不出这一点，用户会以为模型自己想起来要写计划。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatDelta("我把计划排好了：周一到周五各一章。"),
         ChatFinal("我把计划排好了：周一到周五各一章。", "stop", "example", "m", "provider")],
        [ChatDelta("已写入。"), ChatFinal("已写入。", "stop", "example", "m", "provider")],
    ]
    view = _react_turn(client, session_id, script, message="帮我排一下复习计划，周末也要学")

    steps = view["react"]["steps"]
    assert len(steps) == 2, steps
    assert steps[0]["injected"] is None
    assert steps[0]["outcome"] == "remediation"
    assert steps[1]["injected"] == "plan_reminder", steps


def test_long_thinking_is_clipped_and_says_how_long_it_really_was(client):
    """max 档一轮能吐两千 token 的思考。截断可以，装作它本来就这么短不行。"""
    from modules.agent.trace import REACT_FIELD_MAX_CHARS

    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    huge = "想" * (REACT_FIELD_MAX_CHARS * 2)
    script = [[ChatReasoning(huge), ChatDelta("好了。"),
               ChatFinal("好了。", "stop", "example", "m", "provider")]]
    view = _react_turn(client, session_id, script)

    step = view["react"]["steps"][0]
    assert step["reasoning_chars"] == len(huge), "原文多长没说出来"
    assert len(step["reasoning"]) <= REACT_FIELD_MAX_CHARS + 64
    assert step["reasoning"].startswith("想想想")


def test_the_react_text_of_one_turn_has_a_ceiling(client):
    """一轮可以有十几次模型调用。逐轮全存进 payload 文件，一轮就能顶爆单文件上限。"""
    from modules.agent.trace import REACT_FIELD_MAX_CHARS, REACT_TURN_MAX_CHARS

    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    chunk = "思" * REACT_FIELD_MAX_CHARS
    rounds = REACT_TURN_MAX_CHARS // REACT_FIELD_MAX_CHARS + 2
    script = [[ChatReasoning(chunk),
               ChatToolCalls((ToolCallRequest(f"c{index}", "search_materials", '{"query": "护航效应"}'),))]
              for index in range(rounds)]
    script.append([ChatDelta("好了。"), ChatFinal("好了。", "stop", "example", "m", "provider")])
    view = _react_turn(client, session_id, script)

    react = view["react"]
    kept = sum(len(step["reasoning"]) for step in react["steps"] if step["reasoning"])
    assert kept <= REACT_TURN_MAX_CHARS
    assert react["dropped_chars"] > 0, "撞到上限却没说"
    dropped = [step for step in react["steps"] if step["reasoning"] is None and step["reasoning_chars"] > 0]
    assert dropped, "被丢掉的那几段连原本多长都没留下"


def test_the_react_log_survives_a_turn_that_never_reached_an_answer(client):
    """流被打断时这一轮没有回答，但已经走过的几步仍然是排查线索。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [[ChatReasoning("先查教材。"),
               ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "护航效应"}'),))]]

    class Broken(Scripted):
        def chat(self, *, messages, tools=()):
            if self._script:
                yield from self._script.pop(0)
                return
            raise LLMProviderError("upstream_error", "断了", retryable=True)

    space = _space(client)
    space.turns._responder = Broken(script)
    space.turns._fallback_responder = Broken([])
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": "b-1", "message": "护航效应"})

    view = _fetch(client, session_id).json()["turns"][-1]
    assert view["react"]["steps"], "中断的那一轮一步都没记下来"
    assert view["react"]["steps"][0]["calls"] == ["c1"]


# ---- finish_reason（厂商说的）与 outcome（我们判的）分开记 ----

def test_each_step_carries_the_finish_reason_the_provider_returned(client):
    """厂商返回的 finish_reason 原来只进了 SSE 的 turn_completed，没进 trace，
    开发者模式看不到任何一轮是怎么收的。判据走 HTTP，接线漏一处就红。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "护航效应"}'),),
                       provider_finish_reason="tool_calls")],
        [ChatDelta("长作业挡在前面。[1]"),
         ChatFinal("长作业挡在前面。[1]", "stop", "example", "m", "provider", provider_finish_reason="stop")],
    ]
    view = _react_turn(client, session_id, script)

    steps = view["react"]["steps"]
    assert [step["finish_reason"] for step in steps] == ["tool_calls", "stop"], steps


def test_a_provider_that_said_nothing_leaves_finish_reason_null(client):
    """取不到就是 null。兜底填成 stop 会把「厂商没说」伪装成「厂商说正常收尾」。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [[ChatDelta("好了。"), ChatFinal("好了。", "stop", "example", "m", "provider")]]
    view = _react_turn(client, session_id, script)

    assert view["react"]["steps"][0]["finish_reason"] is None


def test_a_server_added_round_keeps_the_providers_own_stop(client):
    """补救轮是服务端补的，厂商无从知道——它那次调用照样正常收尾。
    两个字段合并成一个就会把 stop 覆盖成 remediation，看不出厂商说了什么。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    plan = "我把计划排好了：周一到周五各一章。"
    script = [
        [ChatDelta(plan), ChatFinal(plan, "stop", "example", "m", "provider", provider_finish_reason="stop")],
        [ChatDelta("已写入。"),
         ChatFinal("已写入。", "stop", "example", "m", "provider", provider_finish_reason="stop")],
    ]
    view = _react_turn(client, session_id, script, message="帮我排一下复习计划，周末也要学")

    steps = view["react"]["steps"]
    assert steps[0]["outcome"] == "remediation", steps
    assert steps[0]["finish_reason"] == "stop", steps
    assert steps[1]["injected"] == "plan_reminder", steps


def test_a_round_that_ran_out_of_tool_budget_still_shows_the_providers_tool_calls(client):
    """额度用满是我们的判断，厂商那次返回的是 tool_calls。
    上报给客户端的 finish_reason 是我们编的 tool_budget_exhausted，别把它写进步骤。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)

    class AlwaysCallsTools(Scripted):
        """额度用完那一轮工具定义已经不下发了，它照样在调——这才走到服务端收尾那条分支。"""

        def chat(self, *, messages, tools=()):
            yield ChatDelta("我再查一下。")
            yield ChatToolCalls((ToolCallRequest("x", "list_materials", "{}"),),
                                provider_finish_reason="tool_calls")

    space = _space(client)
    space.turns._responder = AlwaysCallsTools([])
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": "b-1", "message": "有哪些资料"})

    steps = _fetch(client, session_id).json()["turns"][-1]["react"]["steps"]
    exhausted = [step for step in steps if step["outcome"] == "budget_exhausted"]
    assert exhausted, steps
    assert exhausted[-1]["finish_reason"] == "tool_calls", exhausted


def test_the_reasoning_field_name_reaches_the_panel(client):
    """思考内容的字段名不统一（reasoning_content / reasoning），面板要显示实际收到的那个。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [[ChatReasoning("先看看教材。", field="reasoning"), ChatDelta("好了。"),
               ChatFinal("好了。", "stop", "example", "m", "provider", provider_finish_reason="stop")]]
    view = _react_turn(client, session_id, script)

    assert view["react"]["steps"][0]["reasoning_field"] == "reasoning"


# ---- 工具正文改成按需取 ----

def test_the_trace_listing_carries_no_tool_text_at_all(client):
    """打开侧栏是第一眼，不该把几十段检索原文一起拖下来。判据打在响应本身：
    整个 JSON 里不许出现教材原文，每一步只报它有多长。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")

    raw = _fetch(client, session_id).text
    assert "护航效应。SJF" not in raw, "工具正文跟着列表一起下来了"
    view = _fetch(client, session_id).json()["turns"][-1]
    seed = next(tool for tool in view["tools"] if tool["origin"] == "seed")
    assert seed["body"] is not None and seed["body"]["chars"] > 0
    assert "text" not in seed["body"], "列表里还带着正文"


def test_a_tool_body_is_fetched_on_demand_by_call_id(client):
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    view = _fetch(client, session_id).json()["turns"][-1]

    response = client.get(f"/api/v2/sessions/{session_id}/trace/body",
                          params={"turn_id": view["turn_id"], "call_id": SEED_CALL_ID})
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True and "护航效应" in body["text"]
    assert body["chars"] == len(body["text"]) and body["name"] == "search_materials"


def test_fetching_two_calls_of_the_same_tool_never_swaps_their_text(client):
    """同名工具连调几次，按需取那一条也要按 call_id 精确配对。接错一位，
    界面上就是「查 A 拿回了 B 的结果」。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    turn_id = _fetch(client, session_id).json()["turns"][-1]["turn_id"]

    space = _space(client)
    for call_id, text in (("c1", "第一次查回来的"), ("c2", "第二次查回来的")):
        space.sessions.append_message(session_id=session_id, turn_id=turn_id, role="tool",
                                      content=text, activity=[{"call_id": call_id, "name": "search_materials"}])

    assert _body_text(client, session_id, turn_id, "c1") == "第一次查回来的"
    assert _body_text(client, session_id, turn_id, "c2") == "第二次查回来的"


def test_a_tool_that_never_stores_its_output_says_so_instead_of_looking_empty(client):
    """artifact_read、MCP 工具这些按设计不落库。显示成空看起来像出了错，要明说。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    script = [
        [ChatToolCalls((ToolCallRequest("c1", "get_plan", "{}"),))],
        [ChatDelta("看过了。"), ChatFinal("看过了。", "stop", "example", "m", "provider")],
    ]
    view = _react_turn(client, session_id, script, message="我的复习计划到哪了")

    span = next(tool for tool in view["tools"] if tool["name"] == "get_plan")
    assert span["body"] is None
    assert span["body_state"] == "not_persisted", span

    response = client.get(f"/api/v2/sessions/{session_id}/trace/body",
                          params={"turn_id": view["turn_id"], "call_id": "c1"})
    assert response.status_code == 200 and response.json()["found"] is False


def test_the_body_endpoint_cannot_reach_another_users_session(client):
    alice_course = _indexed_course(client, name="甲的操作系统", headers=ALICE)
    alice_session = _session(client, alice_course, headers=ALICE)
    _turn(client, alice_session, request_id="a-1", message="FIFO 为什么有护航效应", headers=ALICE)
    turn_id = _fetch(client, alice_session, headers=ALICE).json()["turns"][-1]["turn_id"]

    params = {"turn_id": turn_id, "call_id": SEED_CALL_ID}
    mine = client.get(f"/api/v2/sessions/{alice_session}/trace/body", params=params, headers=ALICE)
    assert mine.status_code == 200 and mine.json()["found"] is True
    denied = client.get(f"/api/v2/sessions/{alice_session}/trace/body", params=params, headers=BOB)
    assert denied.status_code == 404


def test_the_body_endpoint_will_not_hand_over_another_turns_text(client):
    """turn_id 与 call_id 要一起配对。只按 call_id 找，同一个会话里别的轮次会串过来。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    _turn(client, session_id, request_id="t-2", message="那 SJF 呢")
    ids = _turn_ids(_fetch(client, session_id).json())

    # 两轮里各有一次 call_id 相同的调用：模型自己生成 id，跨轮撞上是常态
    space = _space(client)
    for turn_id, text in zip(ids, ("第一轮的正文", "第二轮的正文")):
        space.sessions.append_message(session_id=session_id, turn_id=turn_id, role="tool",
                                      content=text, activity=[{"call_id": "same", "name": "search_materials"}])

    assert _body_text(client, session_id, ids[0], "same") == "第一轮的正文"
    assert _body_text(client, session_id, ids[1], "same") == "第二轮的正文"


def test_the_focused_turns_payload_is_resolved_before_the_others(client):
    """回读 payload 有总量上限。按文件顺序填会让最新的那几轮全落到 skipped，
    而用户点的多半就是最新那一轮。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    directory = _trace_dir(client)
    day = str(_fetch(client, session_id).json()["turns"][-1]["started_at"])[:10]
    (directory / "payloads").mkdir(parents=True, exist_ok=True)

    chunk = MAX_PAYLOAD_BYTES // 2
    count = MAX_PAYLOAD_TOTAL_BYTES // chunk + 3
    for index in range(count):
        (directory / "payloads" / f"q{index}.json").write_text(
            json.dumps({"tools": {"0.arguments": {"query": "x" * chunk}}}), encoding="utf-8")
    _write_records(directory, day, [_record(
        session_id=session_id, turn_id=f"pay-{index:03d}", started_at=f"{day}T10:{index:02d}:00Z",
        payload_ref=f"payloads/q{index}.json",
        tools=[{"origin": "model", "name": "search_materials", "ok": True,
                "arguments": {"payload_ref": "tools.0.arguments", "chars": chunk}}])
        for index in range(count)])

    last = f"pay-{count - 1:03d}"
    payload = _fetch(client, session_id, turn_id=last).json()
    view = next(item for item in payload["turns"] if item["turn_id"] == last)
    assert view["payload_state"] == "resolved", "点中的那一轮反而被总量闸挡掉了"
