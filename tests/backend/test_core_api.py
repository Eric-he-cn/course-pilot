from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import Settings
from modules.sessions.api import SessionBusyError
from contracts.llm import LLMProviderError, TutorDelta


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir,
        database_path=data_dir / "coursepilot.db",
        uploads_dir=data_dir / "materials",
        text_provider="deepseek",
        text_base_url="https://api.deepseek.com",
        text_api_key="",
        text_model="deepseek-v4-flash",
        enable_remote_llm=False,
        chunk_size=120,
        chunk_overlap=20,
        top_k_results=6,
    )


def _events(body: str) -> list[tuple[str, dict]]:
    frames = [frame for frame in body.split("\n\n") if frame]
    return [(frame.splitlines()[0].removeprefix("event: "), json.loads(frame.splitlines()[1].removeprefix("data: "))) for frame in frames]


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _wait_for_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_general_turn_resolves_per_turn_and_uses_course_scoped_evidence(client):
    calculus = client.post("/api/v2/courses", json={"name": "高等数学 II"}).json()
    physics = client.post("/api/v2/courses", json={"name": "大学物理"}).json()
    assert calculus["color"] != physics["color"]

    material = client.post(
        f"/api/v2/courses/{calculus['id']}/materials",
        files={"file": ("chain-rule.md", "链式法则：复合函数求导时，先对外层求导，再乘以内层导数。", "text/markdown")},
    ).json()
    job = _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    assert job["status"] == "completed"

    session = client.post("/api/v2/sessions", json={"scope_mode": "general", "title": "求导问题"}).json()
    response = client.post(
        f"/api/v2/sessions/{session['id']}/turns",
        json={"client_request_id": "request-1", "message": "高等数学 II 的链式法则怎么用？"},
    )
    assert response.status_code == 200
    events = _events(response.text)
    assert [name for name, _ in events] == ["turn_started", "course_resolution", "citation", "text_delta", "turn_completed"]
    assert events[0][1]["scope_mode"] == "general"
    assert events[1][1]["status"] == "resolved"
    assert events[1][1]["course_id"] == calculus["id"]
    assert events[1][1]["resolved_course_id"] == calculus["id"]
    assert "Demo responder" in events[3][1]["text"]

    messages = client.get(f"/api/v2/sessions/{session['id']}/messages").json()
    assert [item["role"] for item in messages["messages"]] == ["user", "assistant"]
    assert messages["session"]["course_id"] is None
    assert messages["session"]["resolved_course_id"] == calculus["id"]
    assert {item["resolved_course_id"] for item in messages["messages"]} == {calculus["id"]}
    assert {item["resolution_status"] for item in messages["messages"]} == {"resolved"}
    with client.app.state.application.store.read() as connection:
        context = connection.execute("SELECT * FROM turn_course_context").fetchone()
        stored = connection.execute("SELECT course_id FROM sessions WHERE id = ?", (session["id"],)).fetchone()
    assert context["resolved_course_id"] == calculus["id"]
    assert context["created_at"]
    assert stored["course_id"] is None


def test_general_follow_up_reuses_recent_resolution_and_fresh_session_stays_unresolved(client):
    calculus = client.post("/api/v2/courses", json={"name": "高等数学 II"}).json()
    client.post("/api/v2/courses", json={"name": "大学物理"})

    session = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
    first = _events(client.post(f"/api/v2/sessions/{session['id']}/turns", json={"client_request_id": "r-1", "message": "高等数学 II 的链式法则怎么用？"}).text)
    assert first[1][1]["status"] == "resolved"

    follow_up = _events(client.post(f"/api/v2/sessions/{session['id']}/turns", json={"client_request_id": "r-2", "message": "那乘积法则呢？"}).text)
    assert follow_up[1][1]["status"] == "resolved"
    assert follow_up[1][1]["resolved_course_id"] == calculus["id"]
    assert follow_up[1][1]["reason"] == "recent_resolution"

    fresh = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
    vague = _events(client.post(f"/api/v2/sessions/{fresh['id']}/turns", json={"client_request_id": "r-3", "message": "那乘积法则呢？"}).text)
    assert vague[1][1]["status"] == "unresolved"


def test_default_titled_session_is_named_by_first_user_message(client):
    session = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
    assert session["title"] == "新学习对话"
    client.post(f"/api/v2/sessions/{session['id']}/turns", json={"client_request_id": "t-1", "message": "极限的 ε-δ 定义到底怎么理解？我总是记不住量词顺序"})
    titled = client.get(f"/api/v2/sessions/{session['id']}/messages").json()["session"]
    assert titled["title"] == "极限的 ε-δ 定义到底怎么理解？我总是记不住量词顺序"[:30]

    named = client.post("/api/v2/sessions", json={"scope_mode": "general", "title": "求导问题"}).json()
    client.post(f"/api/v2/sessions/{named['id']}/turns", json={"client_request_id": "t-2", "message": "链式法则"})
    kept = client.get(f"/api/v2/sessions/{named['id']}/messages").json()["session"]
    assert kept["title"] == "求导问题"


def test_course_session_is_immutable_and_invalid_general_course_binding_is_rejected(client):
    course = client.post("/api/v2/courses", json={"name": "线性代数"}).json()
    invalid = client.post("/api/v2/sessions", json={"scope_mode": "general", "course_id": course["id"]})
    assert invalid.status_code == 422
    created = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]})
    assert created.status_code == 201
    turn = client.post(f"/api/v2/sessions/{created.json()['id']}/turns", json={"client_request_id": "course-1", "message": "讲讲行列式"})
    events = _events(turn.text)
    assert events[1][1]["status"] == "resolved"
    assert events[1][1]["reason"] == "course_session"


def test_web_cannot_forge_feishu_source_and_feishu_service_session_is_singleton(client):
    rejected = client.post("/api/v2/sessions", json={"scope_mode": "general", "source": "feishu"})
    assert rejected.status_code == 422

    sessions = client.app.state.application.sessions
    first = sessions.create_session(scope_mode="general", course_id=None, title="飞书入口", source="feishu", owner_id="owner-a")
    second = sessions.create_session(scope_mode="general", course_id=None, title="重复投递", source="feishu", owner_id="owner-a")
    another_owner = sessions.create_session(scope_mode="general", course_id=None, title="另一位用户", source="feishu", owner_id="owner-b")
    assert first.id == second.id
    assert another_owner.id != first.id
    assert first.source == "feishu"
    assert first.course_id is None


def test_wiki_job_requires_explicit_flag_and_completed_index(client):
    course = client.post("/api/v2/courses", json={"name": "概率论"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("notes.txt", "随机变量是函数。", "text/plain")},
    ).json()
    disabled = client.post(f"/api/v2/materials/{material['id']}/wiki")
    assert disabled.status_code == 409
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})
    not_indexed = client.post(f"/api/v2/materials/{material['id']}/wiki")
    assert not_indexed.status_code == 409
    assert not_indexed.json()["error"]["code"] == "material_not_indexed"


def test_plan_and_archive_read_skeletons_return_persisted_empty_state(client):
    assert client.get("/api/v2/courses/no-such-course/plan").status_code == 404
    assert client.get("/api/v2/courses/no-such-course/archive").status_code == 404

    course = client.post("/api/v2/courses", json={"name": "常微分方程"}).json()
    plan = client.get(f"/api/v2/courses/{course['id']}/plan")
    assert plan.status_code == 200
    assert plan.json() == {"plan": None}

    archive = client.get(f"/api/v2/courses/{course['id']}/archive")
    assert archive.status_code == 200
    assert archive.json() == {"course_id": course["id"], "evidence_count": 0, "events": []}


def test_errors_use_a_stable_envelope(client):
    response = client.get("/api/v2/jobs/not-a-job")
    assert response.status_code == 404
    assert response.json()["error"] == {"code": "not_found", "message": "任务不存在", "retryable": False}
    invalid = client.post("/api/v2/sessions", json={"scope_mode": "general", "source": "feishu"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_request"


def test_database_enforces_one_active_turn_per_session(client):
    session = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
    sessions = client.app.state.application.sessions
    first, created = sessions.start_turn(session_id=session["id"], client_request_id="active-1")
    assert created is True
    try:
        sessions.start_turn(session_id=session["id"], client_request_id="active-2")
        assert False, "expected the active-turn database constraint"
    except SessionBusyError:
        pass
    sessions.complete_turn(first.id, status="failed")
    next_turn, created = sessions.start_turn(session_id=session["id"], client_request_id="active-2")
    assert created is True
    sessions.complete_turn(next_turn.id, status="completed")


def test_provider_failure_emits_transparent_fallback_and_completes_turn(client):
    class FailingResponder:
        mode = "provider"
        provider = "deepseek"
        model = "deepseek-v4-flash"

        def respond(self, _request):
            raise LLMProviderError("network_error", "unavailable", retryable=True)

        def health(self):
            return {}

        def close(self):
            return None

    course = client.post("/api/v2/courses", json={"name": "离散数学"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("graph.md", "图由顶点集合和边集合构成。", "text/markdown")},
    ).json()
    _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    session = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()
    client.app.state.application.turns._responder = FailingResponder()

    response = client.post(
        f"/api/v2/sessions/{session['id']}/turns",
        json={"client_request_id": "fallback-1", "message": "图由什么构成？"},
    )
    events = _events(response.text)
    assert [name for name, _ in events] == [
        "turn_started", "course_resolution", "citation", "provider_fallback", "text_delta", "turn_completed",
    ]
    assert events[3][1] == {
        "provider": "deepseek", "model": "deepseek-v4-flash", "error_code": "network_error", "retryable": True,
    }
    assert "Demo responder" in events[4][1]["text"]
    assert events[5][1]["responder_mode"] == "demo_fallback"


def test_mid_stream_provider_drop_keeps_partial_answer_and_marks_interrupted(client):
    class InterruptingResponder:
        mode = "provider"
        provider = "deepseek"
        model = "deepseek-v4-flash"

        def respond(self, _request):
            yield TutorDelta("链式法则是复合函数")
            raise LLMProviderError("stream_interrupted", "connection lost", retryable=False)

        def health(self):
            return {}

        def close(self):
            return None

    course = client.post("/api/v2/courses", json={"name": "微积分"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("chain.md", "链式法则：复合函数求导，先外层后内层。", "text/markdown")},
    ).json()
    _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    session = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()
    client.app.state.application.turns._responder = InterruptingResponder()

    response = client.post(f"/api/v2/sessions/{session['id']}/turns", json={"client_request_id": "drop-1", "message": "链式法则？"})
    events = _events(response.text)
    assert [name for name, _ in events] == [
        "turn_started", "course_resolution", "citation", "text_delta", "stream_interrupted", "turn_failed",
    ]
    assert events[4][1] == {"error_code": "stream_interrupted", "retryable": False}
    assert events[5][1]["error_code"] == "stream_interrupted"

    messages = client.get(f"/api/v2/sessions/{session['id']}/messages").json()["messages"]
    assert messages[-1]["content"] == "链式法则是复合函数"
    assert messages[-1]["status"] == "interrupted"

    # 中断后 turn 已落终态，会话立即可以继续
    retry = client.post(f"/api/v2/sessions/{session['id']}/turns", json={"client_request_id": "drop-2", "message": "链式法则？"})
    assert _events(retry.text)[0][0] == "turn_started"


def test_client_disconnect_mid_stream_does_not_brick_the_session(client):
    client.post("/api/v2/courses", json={"name": "高等数学"})
    session = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
    turns = client.app.state.application.turns

    generator = turns.run(session_id=session["id"], message="链式法则", client_request_id="disconnect-1")
    first = next(generator)
    assert first["event"] == "turn_started"
    generator.close()  # 模拟客户端断连：在 yield 处抛 GeneratorExit

    with client.app.state.application.store.read() as connection:
        row = connection.execute("SELECT status FROM turn_requests WHERE client_request_id = 'disconnect-1'").fetchone()
    assert row["status"] == "failed"

    retry = client.post(f"/api/v2/sessions/{session['id']}/turns", json={"client_request_id": "disconnect-2", "message": "继续讲"})
    events = _events(retry.text)
    assert events[-1][0] == "turn_completed"


def test_startup_recovers_stale_running_turns(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings=settings)) as first:
        session = first.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
        turn, created = first.app.state.application.sessions.start_turn(session_id=session["id"], client_request_id="crash-1")
        assert created is True
        # 不 complete，模拟进程在 turn 进行中崩溃

    with TestClient(create_app(settings=settings)) as second:
        recovered_sessions = second.app.state.application.sessions
        next_turn, created = recovered_sessions.start_turn(session_id=session["id"], client_request_id="crash-2")
        assert created is True
        recovered_sessions.complete_turn(next_turn.id, status="completed")


def test_health_reports_enabled_deepseek_adapter_without_exposing_key(tmp_path):
    settings = replace(_settings(tmp_path), text_api_key="test-secret", enable_remote_llm=True)
    with TestClient(create_app(settings=settings)) as remote_client:
        llm = remote_client.get("/api/v2/health").json()["llm"]
    assert llm["configured"] is True
    assert llm["enabled"] is True
    assert llm["adapter_available"] is True
    assert llm["mode"] == "provider"
    assert llm["provider"] == "deepseek"
    assert llm["model"] == "deepseek-v4-flash"
    assert "api_key" not in llm
    assert "test-secret" not in json.dumps(llm)
