from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest
from test_agent_loop import ScriptedChat, _events, _indexed_course_session, _settings

TOOLS_SOURCE = Path(__file__).resolve().parents[2] / "backend" / "modules" / "agent" / "tools.py"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _turn(client: TestClient, session_id: str, *, request_id: str, message: str) -> list[tuple[str, dict]]:
    body = client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": request_id, "message": message}).text
    return _events(body)


def test_tool_result_carries_key_and_args_on_all_three_exits(client):
    """SSE、落库 activity、trace 三条出口都要带 key 与参数。只有一条带上，
    界面切英文就会中英混排；中文 summary 同时保留，供不认识 key 的客户端兜底。"""
    session_id = _indexed_course_session(client, name="微积分", text="链式法则：先对外层求导，再乘以内层导数。")
    scripted = ScriptedChat([
        [ChatToolCalls((ToolCallRequest("c1", "calculator", '{"expression": "1+1"}'),))],
        [ChatDelta("等于 2。"), ChatFinal("等于 2。", "stop", "example", "example-model", "provider")],
    ])
    workspace(client).turns._responder = scripted

    events = _turn(client, session_id, request_id="key-1", message="链式法则怎么用？")
    results = {data["name"]: data for name, data in events if name == "tool_result"}

    seed = results["search_materials"]
    assert seed["summary_key"] == "summary.search_hit"
    assert seed["summary_args"]["query"] == "链式法则怎么用？" and seed["summary_args"]["n"] > 0
    assert seed["summary"].startswith("检索「")  # 中文兜底原样保留

    calc = results["calculator"]
    assert (calc["summary_key"], calc["summary_args"], calc["summary"]) == ("summary.calc", {"expression": "1+1"}, "计算 1+1")

    activity = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"][-1]["activity"]
    assert [entry["summary_key"] for entry in activity] == ["summary.search_hit", "summary.calc"]
    assert activity[-1]["summary_args"] == {"expression": "1+1"}

    traces = sorted((workspace(client).settings.data_dir / "traces").glob("*.jsonl"))
    spans = [span for path in traces for line in path.read_text(encoding="utf-8").splitlines() for span in json.loads(line)["tools"]]
    assert [span["summary_key"] for span in spans] == ["summary.search_hit", "summary.calc"]


def test_denied_tool_reports_a_key_too(client):
    """拒绝路径也上屏。工具名进参数，界面按语言拼句子。"""
    session_id = _indexed_course_session(client, name="操作系统", text="FIFO 调度会产生护航效应。")
    scripted = ScriptedChat([
        [ChatToolCalls((ToolCallRequest("c1", "plan_update", '{"expected_version": 0, "items": []}'),))],
        [ChatDelta("先说建议。"), ChatFinal("先说建议。", "stop", "example", "example-model", "provider")],
    ])
    workspace(client).turns._responder = scripted

    events = _turn(client, session_id, request_id="key-2", message="护航效应是什么？")
    denied = [data for name, data in events if name == "tool_result" and data["name"] == "plan_update"]
    assert denied and denied[0]["summary_key"] == "summary.plan_needs_confirmation"
    assert denied[0]["summary"] == "计划写入需用户确认"


def test_reused_result_keeps_its_key_and_flags_the_reuse(client):
    """复用沿用被复用那次的 key，只多一个 reused 标记，后缀由前端拼。
    把中文摘要塞进参数的话，英文界面会得到「中文原句 + 英文后缀」。"""
    session_id = _indexed_course_session(client, name="概率论", text="全概率公式把事件按一组互斥情形拆开求和。")
    same = '{"query": "全概率公式"}'
    scripted = ScriptedChat([
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", same), ToolCallRequest("c2", "search_materials", same)))],
        [ChatDelta("按互斥情形拆开。[1]"), ChatFinal("按互斥情形拆开。[1]", "stop", "example", "example-model", "provider")],
    ])
    workspace(client).turns._responder = scripted

    events = _turn(client, session_id, request_id="reuse-1", message="全概率公式是什么？")
    results = [data for name, data in events if name == "tool_result" and data["name"] == "search_materials"]
    reused = [data for data in results if data.get("reused")]

    assert len(reused) == 1
    assert reused[0]["summary_key"] == "summary.search_hit"
    assert reused[0]["summary_args"]["query"] == "全概率公式"
    assert reused[0]["summary"].endswith("（与本轮上一次相同，已复用）")
    assert not any("summary" in data["summary_args"] for data in results), "中文摘要不该当成参数传出去"


def test_context_segments_report_key_and_keep_chinese_label(client):
    """上下文段标签同样是 key + 中文两个字段：中文那份还被 e2e 脚本按名字取值。"""
    session_id = _indexed_course_session(client, name="线性代数", text="行列式衡量线性变换对体积的缩放系数。")
    events = _turn(client, session_id, request_id="seg-1", message="行列式是什么？")
    segments = [item for name, data in events if name == "context_usage" for item in data["segments"]]

    assert segments and all(item["label_key"].startswith("context.segment.") for item in segments)
    labeled = {item["label"]: item["label_key"] for item in segments}
    assert labeled["系统提示"] == "context.segment.system"
    assert labeled["教材证据"] == "context.segment.evidence"


def test_general_mode_segments_are_keyed_as_well(client):
    """没有课程的一轮走另一份组装：它的段标签以前是纯中文，漏改就只有这条路径中英混排。"""
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()["id"]
    events = _turn(client, session_id, request_id="seg-2", message="你好")
    labeled = {item["label"]: item["label_key"] for name, data in events if name == "context_usage" for item in data["segments"]}

    assert labeled["系统提示"] == "context.segment.system"
    assert labeled["课程列表"] == "context.segment.courses"


def test_legacy_activity_without_keys_still_reads(client):
    """历史消息只存了中文 summary（不做数据迁移）。读出来不能报错，也不能凭空补 key。"""
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()["id"]
    workspace(client).sessions.append_message(
        session_id=session_id, turn_id=None, role="assistant", content="旧回答",
        activity=[{"call_id": "old", "name": "search_materials", "origin": "model", "ok": True, "summary": "检索「梯度」命中 3 段"}],
    )
    activity = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"][-1]["activity"]

    assert activity == [{"call_id": "old", "name": "search_materials", "origin": "model", "ok": True, "summary": "检索「梯度」命中 3 段"}]
    assert "summary_key" not in activity[0]


def test_auto_loaded_skill_reports_which_reason(client):
    """自动加载的原因三种情况各有一个完整 key。原来原因是拼进中文串里的，
    英文态下这句会剩半截中文；也不做二级插值，那要前端支持两层渲染。"""
    session_id = _indexed_course_session(client, name="微积分", text="链式法则：先对外层求导，再乘以内层导数。")
    workspace(client).turns._responder = ScriptedChat([
        [ChatDelta("好。"), ChatFinal("好。", "stop", "example", "example-model", "provider")],
    ])
    events = _turn(client, session_id, request_id="auto-1", message="画一张流程图")
    loaded = [data for name, data in events if name == "tool_result" and data["name"] == "use_skill"]

    assert loaded, "命中意图应当自动加载 skill"
    assert loaded[0]["summary_key"] == "summary.skill_auto_loaded_intent"
    assert loaded[0]["summary_args"] == {"name": "diagram"}


def test_auto_loaded_practice_reports_a_different_reason(client):
    """练题与命中意图走的是不同分支，key 必须分开——否则界面永远只显示一种原因。"""
    session_id = _indexed_course_session(client, name="微积分", text="链式法则：先对外层求导，再乘以内层导数。")
    workspace(client).turns._responder = ScriptedChat([
        [ChatDelta("好。"), ChatFinal("好。", "stop", "example", "example-model", "provider")],
        [ChatDelta("好。"), ChatFinal("好。", "stop", "example", "example-model", "provider")],
    ])
    events = _turn(client, session_id, request_id="auto-2", message="出一道题")
    loaded = [data for name, data in events if name == "tool_result" and data["name"] == "use_skill"]

    assert loaded, "用户要练题应当自动加载 practice"
    assert loaded[0]["summary_key"] == "summary.skill_auto_loaded_requested"
    assert loaded[0]["summary_args"] == {"name": "practice"}


def test_long_query_is_clipped_before_it_becomes_an_argument(client):
    """截断在后端做，前端不重复截。参数直接上屏，不截就是一整句提问挤爆 chip。"""
    long_question = "这门课里关于链式法则的推导过程和它在反向传播里的具体应用请你详细讲一讲"
    session_id = _indexed_course_session(client, name="微积分", text="链式法则：先对外层求导，再乘以内层导数。")
    workspace(client).turns._responder = ScriptedChat([
        [ChatDelta("好。"), ChatFinal("好。", "stop", "example", "example-model", "provider")],
    ])
    events = _turn(client, session_id, request_id="clip-1", message=long_question)
    seed = next(data for name, data in events if name == "tool_result" and data["name"] == "search_materials")

    assert len(long_question) > 24, "这条测试要求提问长过截断上限"
    assert len(seed["summary_args"]["query"]) == 24 and seed["summary_args"]["query"].endswith("…")


def test_every_tool_outcome_declares_a_summary_key():
    """新增工具结果时忘了给 key，英文界面就会露出一句中文。判据要求 key 是非空字符串
    字面量：只查参数名在不在的话，summary_key=None 也能混过去。唯一的例外是 memory_patch，
    原因写在它自己那处注释里。"""
    source = TOOLS_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # 行号 → 所属函数。不按 FunctionDef 往下走，async def 与模块级的构造点才不会被漏掉。
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner[line] = node.name

    keyless: list[str] = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "ToolOutcome"):
            continue
        given = {keyword.arg: keyword.value for keyword in call.keywords}.get("summary_key")
        if not (isinstance(given, ast.Constant) and isinstance(given.value, str) and given.value):
            keyless.append(f"{owner.get(call.lineno, '<module>')}:{call.lineno}")

    assert [entry.split(":")[0] for entry in keyless] == ["_memory_patch"], f"这些工具结果没有 summary_key：{keyless}"
