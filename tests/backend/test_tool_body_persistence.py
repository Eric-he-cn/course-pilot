"""工具正文落库：检索类工具取回的资料以 role='tool' 留在消息表里。

原来它只在本轮的上下文里活着，下一轮就没了——模型看不到早先轮次查到了什么。
落库之后有三条不能破的性质：读时投影仍然只送 user/assistant、压缩不吃这些行、
界面不把它们画成对话气泡。
"""
from __future__ import annotations

import json
import time

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest
from core.settings import Settings
from modules.agent.context import _budgeted_history
from modules.agent.tools import PERSISTED_TOOL_BODIES, TOOL_BODY_MAX_CHARS, persisted_tool_body

MATERIAL_TEXT = "FIFO 调度下长作业会拖住后面的短作业，这就是护航效应。SJF 优先跑最短的作业。"


def _settings(tmp_path, **extra) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6, **extra,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


class Scripted:
    """按脚本逐次响应，并记下每次收到的 messages。"""

    mode, provider, model = "provider", "example", "example-model"

    def __init__(self, script):
        self._script, self.calls = list(script), []

    def chat(self, *, messages, tools=()):
        self.calls.append(list(messages))
        yield from self._script.pop(0) if self._script else [
            ChatFinal("好的。", "stop", "example", "m", "provider")]

    def health(self):
        return {}

    def close(self):
        return None


def _indexed_course(client, *, name="操作系统", filename="os.md", text=MATERIAL_TEXT) -> str:
    course = client.post("/api/v2/courses", json={"name": name}).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": (filename, text, "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/v2/jobs/{job}").json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    return course["id"]


def _answer(client, session_id, *, request_id, message, script=()) -> Scripted:
    scripted = Scripted(script)
    workspace(client).turns._responder = scripted
    client.post(f"/api/v2/sessions/{session_id}/turns",
                json={"client_request_id": request_id, "message": message})
    return scripted


def _rows(client, session_id: str, role: str) -> list[tuple[str, str]]:
    """(activity_json, content)，按落库顺序。"""
    with workspace(client).store.read() as connection:
        return [(row["activity_json"], row["content"]) for row in connection.execute(
            "SELECT activity_json, content FROM messages WHERE session_id = ? AND role = ? "
            "ORDER BY created_at ASC, rowid ASC", (session_id, role))]


def _plain(reply: str):
    return [[ChatDelta(reply), ChatFinal(reply, "stop", "example", "m", "provider")]]


# ---- 落库本身 ----

def test_a_retrieval_turn_leaves_the_tool_body_in_the_messages_table(client):
    """一轮检索之后，库里要有 role='tool' 的行，正文完整（不是摘要）。"""
    course_id = _indexed_course(client)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id}).json()["id"]

    _answer(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应",
            script=_plain("因为长作业挡在前面。[1]"))

    rows = _rows(client, session_id, "tool")
    assert len(rows) == 1, f"种子检索的正文没落库：{rows}"
    activity, content = rows[0]
    assert json.loads(activity) == [{"call_id": "call_seed_search", "name": "search_materials"}]
    assert "护航效应" in content and "os.md" in content, "落的是摘要不是正文"


def test_the_answer_key_never_lands_in_the_messages_table(client):
    """artifact_append 存的是标准答案与评分要点。它的回执进消息表，用户翻历史就看见了。"""
    course_id = _indexed_course(client)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id}).json()["id"]
    secret = {"kind": "practice_key", "visibility": "model_private", "payload": {"answer": "正确答案是 B"}}
    script = [
        [ChatToolCalls((ToolCallRequest("c1", "artifact_append", json.dumps(secret, ensure_ascii=False)),))],
        [ChatDelta("题目在上面。"), ChatFinal("题目在上面。", "stop", "example", "m", "provider")],
    ]

    _answer(client, session_id, request_id="t-1", message="出一道题考我", script=script)

    bodies = [content for _activity, content in _rows(client, session_id, "tool")]
    assert bodies, "前置条件不成立：这一轮连种子检索的正文都没落库"
    assert all("正确答案" not in body and "practice_key" not in body for body in bodies), bodies
    assert all("artifact_append" not in activity for activity, _content in _rows(client, session_id, "tool"))


def test_only_the_allowlisted_tools_are_persisted():
    """名单是判断标准的落地：写工具的回执、含私有产物的读取、以及会自我复制的
    history_read 都不能进来。"""
    assert persisted_tool_body("search_materials", "教材片段") == "教材片段"
    for name in ("artifact_append", "artifact_read", "history_read", "plan_update", "get_plan",
                 "get_archive", "memory_patch", "note_write", "note_read", "emit_evidence",
                 "use_skill", "ask_user", "calculator", "list_materials"):
        assert persisted_tool_body(name, "内容") is None, f"{name} 不该落库"
    assert PERSISTED_TOOL_BODIES == {
        "search_materials", "concept_search", "wiki_index", "wiki_read", "web_search", "web_fetch"}


def test_an_oversized_body_is_clipped_and_says_so():
    """web_fetch 能抓回很长的网页；一条就把会话表撑起来。"""
    body = persisted_tool_body("web_fetch", "长" * (TOOL_BODY_MAX_CHARS * 2))
    assert body is not None and len(body) <= TOOL_BODY_MAX_CHARS
    assert "末尾已截断" in body


def test_a_denied_or_empty_result_is_not_persisted():
    """"本轮已用满 5 次"这类回执没有回看价值。"""
    assert persisted_tool_body("search_materials", "   ") is None


# ---- 读时投影行为不变 ----

HISTORY = [
    ("user", "FIFO 为什么有护航效应"),
    ("assistant", "因为长作业挡在前面。"),
    ("user", "那 SJF 呢"),
    ("assistant", "SJF 先跑最短的。"),
    ("user", "再讲讲 STCF"),
    ("assistant", "STCF 是 SJF 的抢占版。"),
]
TOOL_ROWS = [("tool", "[1] 文档：os.md，第 1 页；片段：chunk-1\n" + MATERIAL_TEXT * 20)]


@pytest.mark.parametrize("budget", [10_000, 40, 12, 1])
def test_interleaving_tool_rows_does_not_change_the_projection(budget):
    """判据：同一份对话，插进工具正文行之后，_budgeted_history 的三项产出逐条相同。

    工具正文和对话在同一张表里，读历史时一起被取出来。它们既不能进上下文，
    也不能算进"丢了几条"——那个数字是报给用户看的。
    """
    plain = _budgeted_history(HISTORY, budget)
    mixed_history = [item for pair in zip(HISTORY, TOOL_ROWS * len(HISTORY)) for item in pair]
    mixed = _budgeted_history(mixed_history, budget)

    assert [(m.role, m.content) for m in plain[0]] == [(m.role, m.content) for m in mixed[0]]
    assert plain[1:] == mixed[1:], f"dropped/clipped 变了：纯对话 {plain[1:]}，混进工具行 {mixed[1:]}"


def test_the_projection_still_only_carries_what_both_sides_said():
    kept, _dropped, _clipped = _budgeted_history(HISTORY + TOOL_ROWS, 10_000)
    assert {item.role for item in kept} == {"user", "assistant"}
    assert all(MATERIAL_TEXT not in item.content for item in kept)


def test_a_later_turn_sees_no_tool_body_in_its_history_but_can_read_it_back(client):
    """整条链路：第一轮检索的正文不进第二轮的历史，但 history_read 取得回来。"""
    course_id = _indexed_course(client)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id}).json()["id"]
    _answer(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应",
            script=_plain("因为长作业挡在前面。[1]"))

    read_back = [
        [ChatToolCalls((ToolCallRequest("h1", "history_read", '{"turns": 2}'),))],
        [ChatDelta("就是那段。"), ChatFinal("就是那段。", "stop", "example", "m", "provider")],
    ]
    scripted = _answer(client, session_id, request_id="t-2", message="你刚才查到的原文是什么",
                       script=read_back)

    first_request = scripted.calls[0]
    history = [item for item in first_request if item.role in {"user", "assistant"}]
    assert all("chunk-" not in item.content for item in history), "工具正文漏进了跨轮历史"
    replay = next(item.content for item in reversed(scripted.calls[-1])
                  if item.role == "tool" and item.tool_call_id == "h1")
    assert "护航效应" in replay and "os.md" in replay
    # 只有落库的工具正文带「；片段：<chunk_id>」这一段；引用摘要没有。
    assert "；片段：" in replay, f"回放的是引用摘要而不是落库的工具正文：{replay[:400]}"


# ---- 压缩不吃 tool 行 ----

def test_compaction_never_summarizes_a_tool_body(tmp_path):
    """摘要提示词里出现工具正文，就等于把检索原文压成了对话内容——它本来就不在对话里。"""
    settings = _settings(tmp_path, agent_history_token_budget=200, agent_compact_threshold_ratio=0.1)
    with TestClient(create_app(settings=settings)) as live:
        course_id = _indexed_course(live)
        session_id = live.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id}).json()["id"]

        compact_prompts: list[str] = []

        class Recorder(Scripted):
            def chat(self, *, messages, tools=()):
                if any("<summary>" in item.content for item in messages if item.role == "system"):
                    compact_prompts.append(messages[-1].content)
                    yield ChatFinal("<summary>摘要正文</summary>", "stop", "example", "m", "provider")
                    return
                self.calls.append(list(messages))
                yield ChatDelta("好的。")
                yield ChatFinal("好的。", "stop", "example", "m", "provider")

        for index in range(5):
            workspace(live).turns._responder = Recorder([])
            live.post(f"/api/v2/sessions/{session_id}/turns", json={
                "client_request_id": f"c-{index}",
                "message": f"第 {index} 问：护航效应到底是什么意思，能不能连着调度算法一起讲清楚一点",
            })

        assert compact_prompts, "前置条件不成立：压缩没触发，这条测试什么都没验"
        with workspace(live).store.read() as connection:
            tool_rows = connection.execute(
                "SELECT count(*) FROM messages WHERE session_id = ? AND role = 'tool'", (session_id,)).fetchone()[0]
        assert tool_rows, "前置条件不成立：没有工具正文落库"
        for prompt in compact_prompts:
            assert "chunk-" not in prompt and "文档：os.md" not in prompt, prompt[:400]


# ---- 界面不画它 ----

def test_the_messages_endpoint_hides_tool_bodies(client):
    """前端按这个接口画气泡。工具正文出现在返回里，检索原文就会摊满整个会话。"""
    course_id = _indexed_course(client)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id}).json()["id"]
    _answer(client, session_id, request_id="t-1", message="FIFO 为什么有护航效应",
            script=_plain("因为长作业挡在前面。[1]"))

    assert _rows(client, session_id, "tool"), "前置条件不成立：库里没有工具正文"
    payload = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"]
    assert [item["role"] for item in payload] == ["user", "assistant"]
    assert all("chunk-" not in item["content"] for item in payload)
