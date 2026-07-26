from __future__ import annotations

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, LLMProviderError, ToolCallRequest
from core.settings import Settings
from modules.agent.compact import extract_summary


def _settings(tmp_path, *, compact_ratio: float = 0.7) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="https://api.example.com/v1", text_api_key="",
        text_model="example-model", enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
        agent_history_token_budget=2_000, agent_compact_threshold_ratio=compact_ratio,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


class Responder:
    """辅导轮次固定回一句；压缩调用按提示词特征识别，单独给脚本。"""

    mode = "provider"
    provider = "example"
    model = "example-model"

    def __init__(self, compact_script=None):
        self._compact_script = compact_script
        self.compact_calls: list[str] = []

    def chat(self, *, messages, tools=()):
        if any("<summary>" in message.content for message in messages if message.role == "system"):
            self.compact_calls.append(messages[-1].content)
            if self._compact_script is None:
                yield ChatFinal("<summary>压缩后的摘要正文</summary>", "stop", "example", "example-model", "provider")
                return
            yield from self._compact_script()
            return
        yield ChatDelta("好的。")
        yield ChatFinal("好的。", "stop", "example", "example-model", "provider")

    def health(self):
        return {}

    def close(self):
        return None


def _session(client: TestClient) -> str:
    course = client.post("/api/v2/courses", json={"name": "算法"}).json()
    return client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]


def _talk(client: TestClient, session_id: str, *, rounds: int, chars: int = 700) -> None:
    for index in range(rounds):
        client.post(
            f"/api/v2/sessions/{session_id}/turns",
            json={"client_request_id": f"c-{index}", "message": f"第{index}轮：" + "问" * chars},
        )


def test_compaction_replaces_early_messages_with_a_summary(client):
    responder = Responder()
    workspace(client).turns._responder = responder
    session_id = _session(client)

    _talk(client, session_id, rounds=4)

    summary = workspace(client).turns._compactions.latest(session_id=session_id)
    assert summary is not None, "历史超过阈值后应该产生摘要"
    assert summary.summary_text == "压缩后的摘要正文"
    assert summary.covers_message_count > 0
    assert responder.compact_calls, "压缩调用应该真的发生过"

    # 下一轮：水位之前的消息不再以原文进上下文，摘要进系统提示。
    responder.compact_calls.clear()
    events = client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "after", "message": "继续讲"}).text
    assert "compacted_messages" in events
    assert '"chars"' in events


def test_summary_is_injected_and_early_history_is_dropped(client):
    responder = Responder()
    workspace(client).turns._responder = responder
    session_id = _session(client)
    _talk(client, session_id, rounds=4)

    seen: list[list] = []
    original = responder.chat

    def spy(*, messages, tools=()):
        if not any("<summary>" in message.content for message in messages if message.role == "system"):
            seen.append(list(messages))
        yield from original(messages=messages, tools=tools)

    responder.chat = spy  # type: ignore[method-assign]
    client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "next", "message": "继续讲"})

    system = seen[0][0].content
    assert "压缩后的摘要正文" in system
    # 第 0 轮的原文已由摘要代表，不再逐字进上下文。
    assert not any("第0轮" in message.content for message in seen[0][1:])


@pytest.mark.parametrize(
    "script, why",
    [
        (lambda: iter([ChatFinal("忘了包标签的裸文本", "stop", "example", "m", "provider")]), "解析不出 summary 标签"),
        (lambda: iter([ChatFinal("<summary>   </summary>", "stop", "example", "m", "provider")]), "摘要为空"),
        (lambda: iter([ChatToolCalls((ToolCallRequest("x", "list_materials", "{}"),))]), "违规调用工具"),
    ],
)
def test_unusable_summary_is_not_persisted(client, script, why):
    """摘要一旦落库，水位就永久生效、那批原文再也不进上下文，所以宁可不压。"""
    workspace(client).turns._responder = Responder(compact_script=script)
    session_id = _session(client)
    _talk(client, session_id, rounds=4)

    assert workspace(client).turns._compactions.latest(session_id=session_id) is None, why


def test_compaction_failure_does_not_fail_the_turn(client):
    def boom():
        raise LLMProviderError("upstream_error", "供应商挂了", retryable=True)
        yield  # pragma: no cover - 使其成为生成器

    workspace(client).turns._responder = Responder(compact_script=boom)
    session_id = _session(client)
    _talk(client, session_id, rounds=3)

    body = client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": "last", "message": "再来一轮" + "问" * 700}).text
    assert "event: turn_completed" in body
    assert "event: turn_failed" not in body
    assert workspace(client).turns._compactions.latest(session_id=session_id) is None


def test_watermark_only_moves_forward(client):
    store = workspace(client).turns._compactions
    session_id = _session(client)
    message = workspace(client).sessions.append_message(
        session_id=session_id, turn_id=None, role="user", content="一条消息",
    )
    common = dict(
        session_id=session_id, covers_through_message_id=message.id,
        covers_message_count=1, prompt_version="compact_v1", turn_id=None,
    )
    assert store.append(summary_text="新的", covers_through_created_at="2026-07-26T10:00:00+00:00", **common)
    # 较老的水位不能覆盖较新的，否则部分内容会同时出现在摘要和原文里。
    assert not store.append(summary_text="旧的", covers_through_created_at="2026-07-26T09:00:00+00:00", **common)
    assert store.latest(session_id=session_id).summary_text == "新的"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("<summary>正文</summary>", "正文"),
        ("<analysis>思考</analysis>\n<summary>\n正文\n</summary>", "正文"),
        ("没有标签", None),
        ("<summary></summary>", None),
        ("<analysis>只有分析</analysis>", None),
    ],
)
def test_extract_summary_only_trusts_the_summary_block(text, expected):
    assert extract_summary(text) == expected


def test_session_rename(client):
    session_id = _session(client)
    renamed = client.patch(f"/api/v2/sessions/{session_id}", json={"title": "  期末冲刺  "})
    assert renamed.status_code == 200 and renamed.json()["title"] == "期末冲刺"

    assert client.patch(f"/api/v2/sessions/{session_id}", json={"title": "   "}).status_code == 422
    assert client.patch("/api/v2/sessions/session_missing", json={"title": "x"}).status_code == 404
    # 与建会话一致：超长截断而不是拒绝。
    long_title = client.patch(f"/api/v2/sessions/{session_id}", json={"title": "长" * 200}).json()["title"]
    assert len(long_title) == 120
