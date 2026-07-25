from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.llm import ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, ToolCallRequest
from core.settings import Settings


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="deepseek", text_base_url="https://api.deepseek.com", text_api_key="",
        text_model="deepseek-v4-flash", enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _events(body: str) -> list[tuple[str, dict]]:
    frames = [frame for frame in body.split("\n\n") if frame]
    return [(frame.splitlines()[0].removeprefix("event: "), json.loads(frame.splitlines()[1].removeprefix("data: "))) for frame in frames]


def _indexed_course_session(client: TestClient, *, name: str, text: str) -> str:
    course = client.post("/api/v2/courses", json={"name": name}).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials", files={"file": ("notes.md", text, "text/markdown")}).json()
    job_id = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if client.get(f"/api/v2/jobs/{job_id}").json()["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    return client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]


class ScriptedChat:
    """按预设脚本逐次响应；记录每次收到的 messages，用于断言上下文组装。"""

    mode = "provider"
    provider = "deepseek"
    model = "deepseek-v4-flash"

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


def test_model_search_loops_and_reuses_citation_numbering(client):
    session_id = _indexed_course_session(client, name="微积分", text="链式法则：复合函数求导时，先对外层求导，再乘以内层导数。")
    scripted = ScriptedChat([
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "链式法则"}'),))],
        [ChatDelta("先外层后内层。[1]"), ChatFinal("先外层后内层。[1]", "stop", "deepseek", "deepseek-v4-flash", "provider")],
    ])
    client.app.state.application.turns._responder = scripted

    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "loop-1", "message": "链式法则怎么用？"}).text)
    names = [name for name, _ in events]

    # 种子检索 + 模型再查一次 = 2 次工具调用，但命中同一 chunk，引用只编号一次。
    assert names.count("tool_call") == 2
    assert names.count("tool_result") == 2
    assert names.count("citation") == 1
    origins = [data["origin"] for name, data in events if name == "tool_call"]
    assert origins == ["seed", "model"]
    assert events[-1][1]["tool_rounds"] == 1

    persisted = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"]
    assert len(persisted[-1]["citations"]) == 1


def test_only_cited_evidence_is_persisted(client):
    text = "\n\n".join(f"第 {i} 节：向量范数用于衡量向量长度，编号 {i}。" for i in range(1, 6))
    session_id = _indexed_course_session(client, name="数值分析", text=text)
    scripted = ScriptedChat([[ChatDelta("只用了第一条证据。[1]"), ChatFinal("只用了第一条证据。[1]", "stop", "deepseek", "deepseek-v4-flash", "provider")]])
    client.app.state.application.turns._responder = scripted

    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "cite-1", "message": "向量范数是什么？"}).text)
    retrieved = [name for name, _ in events].count("citation")
    assert retrieved > 1  # 种子检索命中多段

    citations = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"][-1]["citations"]
    assert [citation["number"] for citation in citations] == [1]


def test_prior_messages_are_injected_into_the_prompt(client):
    session_id = _indexed_course_session(client, name="线性代数", text="行列式衡量线性变换对体积的缩放系数。")
    # 第一轮用默认 demo responder，产生历史。
    client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "h-1", "message": "行列式是什么？"})

    scripted = ScriptedChat([[ChatDelta("好的。"), ChatFinal("好的。", "stop", "deepseek", "deepseek-v4-flash", "provider")]])
    client.app.state.application.turns._responder = scripted
    client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "h-2", "message": "那它和特征值有关吗？"})

    seen = [(message.role, message.content) for message in scripted.calls[0]["messages"]]
    assert ("user", "行列式是什么？") in seen  # 上一轮用户提问进入历史
    assert any(role == "user" and "那它和特征值有关吗？" in content for role, content in seen)  # 本轮问题
    assert any(role == "assistant" and content for role, content in seen)  # 上一轮助手回答也在


def test_plan_and_archive_tools_report_empty_state(client):
    session_id = _indexed_course_session(client, name="概率论", text="随机变量是样本空间到实数的可测函数。")
    scripted = ScriptedChat([
        [ChatToolCalls((ToolCallRequest("p1", "get_plan", "{}"), ToolCallRequest("a1", "get_archive", "{}")))],
        [ChatDelta("暂无计划与记录。"), ChatFinal("暂无计划与记录。", "stop", "deepseek", "deepseek-v4-flash", "provider")],
    ])
    client.app.state.application.turns._responder = scripted

    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "tool-1", "message": "我的复习计划到哪了？"}).text)
    summaries = [data["summary"] for name, data in events if name == "tool_result"]
    assert "暂无计划" in summaries
    assert "档案为空" in summaries


def test_tool_budget_is_bounded(client):
    session_id = _indexed_course_session(client, name="离散数学", text="图由顶点集合与边集合构成。")

    class AlwaysCallsTools:
        mode = "provider"
        provider = "deepseek"
        model = "deepseek-v4-flash"

        def chat(self, *, messages, tools=()):
            if tools:
                yield ChatToolCalls((ToolCallRequest("x", "list_materials", "{}"),))
            else:
                yield ChatFinal("已达检索步数上限。", "tool_budget_exhausted", "deepseek", "deepseek-v4-flash", "provider")

        def health(self):
            return {}

        def close(self):
            return None

    client.app.state.application.turns._responder = AlwaysCallsTools()
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "budget-1", "message": "有哪些资料？"}).text)

    assert events[-1][0] == "turn_completed"
    assert events[-1][1]["tool_rounds"] == 6
    assert events[-1][1]["finish_reason"] == "tool_budget_exhausted"
