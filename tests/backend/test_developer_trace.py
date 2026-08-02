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

from app.http.devtools import (MAX_BODY_CHARS, MAX_DAY_FILES, MAX_PAYLOAD_BYTES,
                               MAX_PAYLOAD_TOTAL_BYTES, MAX_SCAN_LINES, MAX_TURNS)
from app.main import create_app
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest
from core.settings import Settings

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
    assert "护航效应" in seed["body"]["text"] and "os.md" in seed["body"]["text"]
    assert seed["body"]["chars"] == len(seed["body"]["text"])


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
    assert any(body["text"] and "护航效应" in body["text"] for body in view["unmatched_bodies"])


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
        assert all("SJF" not in (body.get("text") or "") for body in view["unmatched_bodies"])


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
    assert all(tool["body"] is None or "子任务" not in tool["body"]["text"] for tool in view["tools"])


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


def test_bodies_beyond_the_char_budget_are_marked_not_silently_dropped(client):
    """一个会话几十轮检索，正文加起来能有几兆。裁掉可以，装作没有不行。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    turn_id = _fetch(client, session_id).json()["turns"][-1]["turn_id"]

    space = _space(client)
    for index in range(MAX_BODY_CHARS // 4_000 + 3):
        space.sessions.append_message(session_id=session_id, turn_id=turn_id, role="tool",
                                      content="长" * 4_000, activity=[{"call_id": f"sub:x{index}", "name": "web_fetch"}])

    view = _fetch(client, session_id).json()["turns"][-1]
    kept = sum(len(body["text"]) for body in view["subagent_bodies"] if body["text"] is not None)
    assert kept <= MAX_BODY_CHARS
    assert view["bodies_omitted"] > 0
    omitted = [body for body in view["subagent_bodies"] if body["text"] is None]
    assert omitted and all(body["chars"] > 0 for body in omitted), "裁掉的那些连原本多长都没说"


def test_the_focused_turn_gets_its_bodies_before_the_others(client):
    """预算填满时先填用户点的那一轮，否则点旧轮次永远看不到正文。"""
    course_id = _indexed_course(client)
    session_id = _session(client, course_id)
    _turn(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应")
    _turn(client, session_id, request_id="t-2", message="那 SJF 呢")
    ids = _turn_ids(_fetch(client, session_id).json())

    space = _space(client)
    for turn_id in ids:
        for index in range(MAX_BODY_CHARS // 4_000 + 2):
            space.sessions.append_message(session_id=session_id, turn_id=turn_id, role="tool",
                                          content="长" * 4_000, activity=[{"call_id": f"sub:{turn_id}-{index}", "name": "web_fetch"}])

    payload = _fetch(client, session_id, turn_id=ids[0]).json()
    first = next(item for item in payload["turns"] if item["turn_id"] == ids[0])
    second = next(item for item in payload["turns"] if item["turn_id"] == ids[1])
    assert any(body["text"] for body in first["subagent_bodies"]), "点中的那一轮反而没有正文"
    assert second["bodies_omitted"] > first["bodies_omitted"]


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
    bodies = [{"call_id": "c3", "name": "search_materials", "text": "第三次"},
              {"call_id": "c1", "name": "search_materials", "text": "第一次"},
              {"call_id": "c2", "name": "search_materials", "text": "第二次"}]
    _attach_bodies(view, bodies)

    assert [tool["body"]["text"] for tool in view["tools"]] == ["第一次", "第二次", "第三次"]
    assert view["unmatched_bodies"] == []
