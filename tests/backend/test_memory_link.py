"""memory_patch 到界面读取的完整链路，以及提示词里那条「必须调用」的规则。

现象是模型声称写了 user.md 但界面为空。这里分两层守：存储链路本身通不通（用脚本化
假模型精确控制工具调用），以及提示词有没有明确要求模型在用户说「记住」时真的调工具。
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


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="https://api.example.com/v1", text_api_key="",
        text_model="example-model", enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _events(body: str) -> list[tuple[str, dict]]:
    frames = [frame for frame in body.split("\n\n") if frame]
    return [(frame.splitlines()[0].removeprefix("event: "), json.loads(frame.splitlines()[1].removeprefix("data: "))) for frame in frames]


class ScriptedChat:
    mode, provider, model = "provider", "example", "example-model"

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def chat(self, *, messages, tools=()):
        self.calls.append({"messages": list(messages), "tools": tuple(spec.name for spec in tools)})
        yield from self._script.pop(0)

    def health(self):
        return {}

    def close(self):
        return None


def _course_session(client: TestClient, *, name: str, text: str) -> tuple[str, str]:
    course = client.post("/api/v2/courses", json={"name": name}).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials", files={"file": ("notes.md", text, "text/markdown")}).json()
    job_id = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if client.get(f"/api/v2/jobs/{job_id}").json()["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]
    return course["id"], session_id


def _patch_turn(client, session_id, *, request_id, args, before=(), message="记住我喜欢先给摘要"):
    """跑一轮：模型先按 before 里的调用铺垫，再发一次 memory_patch，然后收尾。"""
    calls = [*before, ToolCallRequest("m1", "memory_patch", json.dumps(args, ensure_ascii=False))]
    scripted = ScriptedChat(
        [[ChatToolCalls((call,))] for call in calls]
        + [[ChatDelta("已经记住了。"), ChatFinal("已经记住了。", "stop", "example", "example-model", "provider")]]
    )
    workspace(client).turns._responder = scripted
    body = client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": request_id, "message": message}).text
    return _events(body), scripted


def test_user_scope_patch_is_readable_over_http(client):
    _, session_id = _course_session(client, name="操作系统", text="时间片越长，响应时间越差。")
    events, _ = _patch_turn(client, session_id, request_id="mem-user",
                            args={"scope": "user", "section": "preferences", "content": "讲解先给摘要再展开。"})

    patched = [data for name, data in events if name == "tool_result" and data["name"] == "memory_patch"]
    assert patched and patched[0]["ok"], f"memory_patch 被拒：{patched}"
    assert "讲解先给摘要再展开。" in client.get("/api/v2/memory").json()["content"]


def test_course_scope_patch_does_not_land_in_the_user_profile(client):
    course_id, session_id = _course_session(client, name="离散数学", text="图由顶点集合与边集合构成。")
    _patch_turn(client, session_id, request_id="mem-course",
                args={"scope": "course", "section": "progress", "content": "学到图的连通性。"})

    assert "学到图的连通性。" in client.get(f"/api/v2/courses/{course_id}/memory").json()["content"]
    assert "学到图的连通性。" not in client.get("/api/v2/memory").json()["content"]


def test_manual_edit_and_patch_share_one_file(client):
    """用户在界面上手写的段落与 memory_patch 的受管区块必须落在同一份文件里。"""
    _, session_id = _course_session(client, name="概率论", text="随机变量是样本空间到实数的可测函数。")
    client.put("/api/v2/memory", json={"content": "# 用户画像\n\n我是转专业的。"})
    _patch_turn(client, session_id, request_id="mem-merge",
                args={"scope": "user", "section": "goals", "content": "目标是期末上 85。"})

    content = client.get("/api/v2/memory").json()["content"]
    assert "我是转专业的。" in content and "目标是期末上 85。" in content


def test_written_memory_comes_back_in_the_next_turn_prompt(client):
    """模型确认自己记了什么只能靠提示词里的记忆段——写进去的内容必须在下一轮出现。"""
    _, session_id = _course_session(client, name="信号与系统", text="卷积把冲激响应与输入序列结合起来。")
    _patch_turn(client, session_id, request_id="mem-echo",
                args={"scope": "user", "section": "preferences", "content": "讲解先给摘要再展开。"})

    _, scripted = _patch_turn(client, session_id, request_id="mem-echo-2",
                              args={"scope": "user", "section": "goals", "content": "目标是期末上 85。"})
    system = scripted.calls[0]["messages"][0].content
    assert "讲解先给摘要再展开。" in system


def test_tool_result_never_reports_success_without_a_real_write(client):
    """skill 激活会把工具集换成它声明的那套，memory_patch 可能整个不在里面。
    无论放行还是拒绝，工具结果与用户读到的内容必须一致，不能出现静默失败。"""
    _, session_id = _course_session(client, name="微积分", text="链式法则：先对外层求导，再乘内层导数。")
    events, _ = _patch_turn(
        client, session_id, request_id="mem-skill",
        args={"scope": "user", "section": "preferences", "content": "喜欢先看结论。"},
        before=(ToolCallRequest("s1", "use_skill", '{"name": "flashcards"}'),),
    )
    patched = [data for name, data in events if name == "tool_result" and data["name"] == "memory_patch"]
    stored = "喜欢先看结论。" in client.get("/api/v2/memory").json()["content"]
    assert patched and patched[0]["ok"] == stored, f"工具结果与实际写入不符：{patched}，stored={stored}"


def test_prompt_requires_calling_memory_patch_when_asked_to_remember():
    """模型不会主动调工具是这个项目反复踩的坑：这条规则必须是硬要求，
    并且要把「记忆就是这段文本」讲清楚，否则模型会编一套存储实现来自圆其说。"""
    from modules.agent.context import assemble_messages

    system = assemble_messages(
        course_name="测试", materials=["a.md"], history=[], question="记住我喜欢先看结论",
        seed_query="q", seed_result_text="e", history_token_budget=1000,
    ).messages[0].content
    mandate = [line for line in system.splitlines() if "memory_patch" in line and "必须" in line]
    assert mandate, "提示词里没有「必须调用 memory_patch」这样的硬要求"
    assert "已记住" in system  # 未成功调用不得声称记住
