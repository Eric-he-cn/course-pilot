"""删除会话 / 教材 / 课程：核心断言是 PRAGMA foreign_key_check 为空。

26 条外键全是 NO ACTION，删除顺序错了要么直接报错，要么留下孤儿行；
foreign_key_check 比逐表 count 更能兜住"漏了一张表"。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import Settings
from modules.sessions.artifacts import ArtifactStore


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


def _assert_no_orphans(client: TestClient) -> None:
    with workspace(client).store.read() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _count(client: TestClient, sql: str, params: tuple = ()) -> int:
    with workspace(client).store.read() as connection:
        return int(connection.execute(sql, params).fetchone()[0])


# 概念抽取会把只出现在少数分片里的标题当概念，所以教材要长到能切成多片。
_MATERIAL_TEXT = "\n\n".join((
    "# 链式法则", "复合函数求导时，先对外层求导，再乘以内层导数。" * 4,
    "# 隐函数求导", "对方程两边同时求导，再解出所求的导数。" * 4,
    "# 换元积分法", "把被积表达式换成新的变量，积分随之变简单。" * 4,
))


def _indexed_material(client: TestClient, course_id: str, *, filename: str = "chain-rule.md") -> dict:
    material = client.post(
        f"/api/v2/courses/{course_id}/materials",
        files={"file": (filename, _MATERIAL_TEXT, "text/markdown")},
    ).json()
    job_id = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            assert job["status"] == "completed"
            return material
        time.sleep(0.01)
    raise AssertionError("索引任务未完成")


def _turn(client: TestClient, session_id: str, message: str, request_id: str) -> None:
    assert client.post(f"/api/v2/sessions/{session_id}/turns", json={"client_request_id": request_id, "message": message}).status_code == 200


def _compact(client: TestClient, session_id: str) -> None:
    """压缩摘要同时引用 sessions 与 messages，是删除顺序最容易踩的一条。"""
    application = workspace(client)
    messages = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"]
    last = messages[-1]
    with application.store.write() as connection:
        connection.execute(
            "INSERT INTO session_compactions(id, session_id, covers_through_message_id, covers_through_created_at,"
            " covers_message_count, summary_text, prompt_version, turn_id, created_at)"
            " VALUES ('compaction_test', ?, ?, ?, ?, '摘要', 'v1', NULL, ?)",
            (session_id, last["id"], last["created_at"], len(messages), last["created_at"]),
        )


def test_delete_session_removes_messages_turns_and_artifacts(client):
    course = client.post("/api/v2/courses", json={"name": "高等数学 II"}).json()
    session = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()
    _turn(client, session["id"], "链式法则怎么用？", "request-1")
    _compact(client, session["id"])
    application = workspace(client)
    ArtifactStore(application.store).append(course_id=course["id"], session_id=session["id"], kind="practice", visibility="user_visible", payload={"practice_id": "p1"})
    assert _count(client, "SELECT count(*) FROM messages WHERE session_id = ?", (session["id"],)) == 2

    assert client.delete(f"/api/v2/sessions/{session['id']}").status_code == 204

    _assert_no_orphans(client)
    assert client.get(f"/api/v2/sessions/{session['id']}/messages").status_code == 404
    for table in ("messages", "turn_requests", "attachments", "artifacts", "session_compactions"):
        assert _count(client, f"SELECT count(*) FROM {table} WHERE session_id = ?", (session["id"],)) == 0
    assert _count(client, "SELECT count(*) FROM turn_course_context") == 0
    # 课程不受连带影响。
    assert client.get("/api/v2/courses").json()[0]["id"] == course["id"]


def test_delete_material_clears_chunks_concepts_mastery_and_file(client):
    course = client.post("/api/v2/courses", json={"name": "高等数学 II"}).json()
    material = _indexed_material(client, course["id"])
    application = workspace(client)
    concepts = application.knowledge.list_course_concepts(course_id=course["id"])
    assert concepts, "索引后应当产出概念目录"
    concept_id = concepts[0].id
    with application.store.write() as connection:
        connection.execute("INSERT INTO concept_aliases(concept_id, alias) VALUES (?, '别名')", (concept_id,))
    for _ in range(3):
        application.learning.record_evidence(course_id=course["id"], kind="attempt_correct", concept_id=concept_id)
    assert _count(client, "SELECT count(*) FROM concept_mastery WHERE concept_id = ?", (concept_id,)) == 1
    stored_file = application.settings.uploads_dir
    files_before = list(stored_file.iterdir())
    assert files_before

    assert client.delete(f"/api/v2/materials/{material['id']}").status_code == 204

    _assert_no_orphans(client)
    assert client.get(f"/api/v2/courses/{course['id']}/materials").json() == []
    for sql in (
        "SELECT count(*) FROM chunks", "SELECT count(*) FROM chunks_fts", "SELECT count(*) FROM concepts",
        "SELECT count(*) FROM concept_aliases", "SELECT count(*) FROM concept_mastery", "SELECT count(*) FROM jobs",
    ):
        assert _count(client, sql) == 0, sql
    # 原始证据保留：掌握度以后能从事件流重算。
    assert _count(client, "SELECT count(*) FROM evidence_events WHERE course_id = ?", (course["id"],)) == 3
    assert list(stored_file.iterdir()) == []


def test_delete_course_removes_its_content_and_keeps_general_sessions(client):
    course = client.post("/api/v2/courses", json={"name": "高等数学 II"}).json()
    other = client.post("/api/v2/courses", json={"name": "大学物理"}).json()
    _indexed_material(client, course["id"])
    other_material = _indexed_material(client, other["id"], filename="physics.md")

    course_session = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()
    _turn(client, course_session["id"], "链式法则怎么用？", "request-1")
    _compact(client, course_session["id"])
    general = client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()
    _turn(client, general["id"], "高等数学 II 的链式法则怎么用？", "request-2")
    assert client.get(f"/api/v2/sessions/{general['id']}/messages").json()["session"]["resolved_course_id"] == course["id"]

    application = workspace(client)
    application.planning.update_plan(
        course_id=course["id"], expected_version=0,
        items=[{"due_date": (date.today() + timedelta(days=1)).isoformat(), "title": "复习链式法则"}],
    )
    application.learning.record_evidence(course_id=course["id"], kind="follow_up", topic_hint="链式法则")
    ArtifactStore(application.store).append(course_id=course["id"], session_id=general["id"], kind="practice", visibility="user_visible", payload={"practice_id": "p1"})
    application.notes.write(course_id=course["id"], title="学习卡片", content="链式法则")
    application.memory.write_whole(scope="course", course_id=course["id"], content="学到第三章")
    wiki_dir = application.settings.data_dir / "wiki" / course["id"]
    wiki_dir.mkdir(parents=True, exist_ok=True)
    (wiki_dir / "index.md").write_text("wiki", encoding="utf-8")
    directories = [application.settings.data_dir / "notes" / course["id"], wiki_dir, application.settings.data_dir / "courses" / course["id"]]
    assert all(directory.is_dir() for directory in directories)

    assert client.delete(f"/api/v2/courses/{course['id']}").status_code == 204

    _assert_no_orphans(client)
    assert [item["id"] for item in client.get("/api/v2/courses").json()] == [other["id"]]
    assert [item["id"] for item in client.get("/api/v2/sessions").json()] == [general["id"]]
    # 通用会话只丢掉历史解析痕迹。
    assert client.get(f"/api/v2/sessions/{general['id']}/messages").json()["session"]["resolved_course_id"] is None
    for table in ("materials", "chunks", "plans", "evidence_events", "artifacts", "concepts", "jobs"):
        assert _count(client, f"SELECT count(*) FROM {table} WHERE course_id = ?", (course["id"],)) == 0, table
    # 计划条目与改版记录挂在 plans 上，课程删完这两张表整体为空。
    for table in ("plan_items", "plan_revisions"):
        assert _count(client, f"SELECT count(*) FROM {table}") == 0, table
    assert not any(directory.exists() for directory in directories)
    # 另一门课程的教材、分片与检索索引不受影响。
    assert [item["id"] for item in client.get(f"/api/v2/courses/{other['id']}/materials").json()] == [other_material["id"]]
    assert _count(client, "SELECT count(*) FROM chunks_fts WHERE course_id = ?", (other["id"],)) > 0


def test_deleting_missing_ids_returns_404(client):
    assert client.delete("/api/v2/sessions/session_missing").status_code == 404
    assert client.delete("/api/v2/materials/material_missing").status_code == 404
    assert client.delete("/api/v2/courses/course_missing").status_code == 404


def test_deleting_a_course_takes_its_files_with_it(client, tmp_path):
    """笔记、Wiki 页、课程记忆是用户的私人内容，删课程后不该静默留在磁盘上。
    这三处的目录布局各归各的模块，组装根只负责把 delete_course 串起来叫一遍。"""
    course_id = client.post("/api/v2/courses", json={"name": "线性代数"}).json()["id"]
    application = workspace(client)
    application.notes.write(course_id=course_id, title="卡片", content="秩等于主元个数")
    application.memory.patch(scope="course", course_id=course_id, section="focus", content="期末只考前四章")
    wiki = application.knowledge._wiki
    wiki.write(course_id=course_id, concept_id="rank", concept_name="秩", body="正文",
               source_hash="h", source_refs=[], updated_at="2026-07-27T00:00:00+00:00")

    written = [application.notes._course_dir(course_id), wiki._course_dir(course_id),
               application.memory._course_path(course_id).parent]
    assert all(path.exists() for path in written), f"前置写入没落盘：{written}"

    client.delete(f"/api/v2/courses/{course_id}")
    leftover = [str(path) for path in written if path.exists()]
    assert not leftover, f"删课程后这些目录还在：{leftover}"
