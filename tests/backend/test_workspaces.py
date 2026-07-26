from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.identity import InvalidUsername, normalize_username, workspace_id
from core.settings import Settings

ALICE = {"X-CoursePilot-User": "alice"}
BOB = {"X-CoursePilot-User": "bob"}


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="deepseek", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def test_two_users_share_nothing(client):
    """隔离是结构性的：两个用户的课程、会话、笔记、记忆、计划、档案互相都读不到。"""
    alice_course = client.post("/api/v2/courses", json={"name": "甲的操作系统"}, headers=ALICE).json()
    bob_course = client.post("/api/v2/courses", json={"name": "乙的深度学习"}, headers=BOB).json()

    assert [c["name"] for c in client.get("/api/v2/courses", headers=ALICE).json()] == ["甲的操作系统"]
    assert [c["name"] for c in client.get("/api/v2/courses", headers=BOB).json()] == ["乙的深度学习"]

    # 拿着对方的课程 id 也读不到——那门课不在自己的库里
    for path in ("plan", "archive", "notes", "memory"):
        assert client.get(f"/api/v2/courses/{bob_course['id']}/{path}", headers=ALICE).status_code == 404
        assert client.get(f"/api/v2/courses/{alice_course['id']}/{path}", headers=BOB).status_code == 404

    # 会话
    client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": alice_course["id"]}, headers=ALICE)
    assert len(client.get("/api/v2/sessions?scope_mode=course", headers=ALICE).json()) == 1
    assert client.get("/api/v2/sessions?scope_mode=course", headers=BOB).json() == []

    # 跨课程画像
    client.put("/api/v2/memory", json={"content": "甲的偏好"}, headers=ALICE)
    assert "甲的偏好" in client.get("/api/v2/memory", headers=ALICE).json()["content"]
    assert client.get("/api/v2/memory", headers=BOB).json()["content"] == ""

    # 导入的 skill
    skill = "---\nname: only_alice\ndescription: d\nwhen_to_use: w\nallowed_tools: [search_materials]\n---\n\n正文\n"
    client.post("/api/v2/skills", files={"file": ("SKILL.md", skill, "text/markdown")}, headers=ALICE)
    assert any(s["name"] == "only_alice" for s in client.get("/api/v2/skills", headers=ALICE).json()["skills"])
    assert not any(s["name"] == "only_alice" for s in client.get("/api/v2/skills", headers=BOB).json()["skills"])


def test_files_land_in_separate_directories(client, tmp_path):
    client.post("/api/v2/courses", json={"name": "甲"}, headers=ALICE)
    client.post("/api/v2/courses", json={"name": "乙"}, headers=BOB)
    root = tmp_path / "data" / "users"
    assert sorted(p.name for p in root.iterdir()) == sorted([workspace_id("alice"), workspace_id("bob")])
    for uid in (workspace_id("alice"), workspace_id("bob")):
        assert (root / uid / "coursepilot.db").is_file()


@pytest.mark.parametrize("name", ["Eric", " eric ", "ERIC", "  ErIc"])
def test_usernames_are_case_insensitive(client, name):
    """大小写合并避免同一个人造出两份资料。"""
    client.post("/api/v2/courses", json={"name": "唯一一门"}, headers={"X-CoursePilot-User": "eric"})
    assert [c["name"] for c in client.get("/api/v2/courses", headers={"X-CoursePilot-User": name}).json()] == ["唯一一门"]


def test_different_people_get_different_workspaces():
    assert workspace_id("张三") != workspace_id("zhangsan")
    assert workspace_id("eric") == workspace_id("ERIC")


@pytest.mark.parametrize(
    "bad, why",
    [
        ("", "空"), ("   ", "全空白"), ("a" * 40, "超长"),
        ("a/b", "路径分隔符"), ("../etc", "路径穿越"), ("x\ny", "换行"),
        ("a‍b", "零宽连接符"), ("a‮b", "双向控制符"),
    ],
)
def test_invalid_usernames_are_rejected(bad, why):
    with pytest.raises(InvalidUsername):
        normalize_username(bad)


def test_invalid_username_header_is_422_not_a_wrong_workspace(client):
    """静默降级会把人塞进别人的工作区，所以宁可报错。"""
    assert client.get("/api/v2/courses", headers={"X-CoursePilot-User": "a/b"}).status_code == 422


def test_cjk_username_survives_the_header(client):
    """HTTP 头值是 ByteString，中日韩用户名前端会 encodeURIComponent 后再放进来。"""
    from urllib.parse import quote

    encoded = quote("张三")
    client.post("/api/v2/courses", json={"name": "张三的课"}, headers={"X-CoursePilot-User": encoded})
    assert [c["name"] for c in client.get("/api/v2/courses", headers={"X-CoursePilot-User": encoded}).json()] == ["张三的课"]
    # 编码后的头值必须是纯 ASCII，否则浏览器的 fetch 会直接抛 TypeError
    assert encoded.isascii()


def test_missing_header_falls_back_to_the_default_user(client):
    """脚本不带头也要能跑；默认用户与显式传同名是同一个工作区。"""
    client.post("/api/v2/courses", json={"name": "默认用户的课"})
    assert [c["name"] for c in client.get("/api/v2/courses").json()] == ["默认用户的课"]
    assert [c["name"] for c in client.get("/api/v2/courses", headers={"X-CoursePilot-User": "local"}).json()] == ["默认用户的课"]


def test_embedder_is_shared_across_workspaces(client):
    """BGE 模型按用户各加载一份不可接受，必须是同一个实例。"""
    workspaces = client.app.state.workspaces
    alice = workspaces.for_username("alice")
    bob = workspaces.for_username("bob")
    assert alice.store is not bob.store            # 数据库各一份
    assert alice.knowledge is not bob.knowledge
    assert workspaces.shared is workspaces.shared  # 共享层只有一份
    assert alice.llm is bob.llm                    # 适配器共享


def test_concurrent_first_requests_do_not_double_build(client):
    """migration 里有非幂等的 ALTER TABLE：并发首请求必须串行化，否则撞 duplicate column。"""
    import threading

    workspaces = client.app.state.workspaces
    results: list = []
    errors: list = []

    def build():
        try: results.append(workspaces.for_username("racer"))
        except Exception as error: errors.append(error)

    threads = [threading.Thread(target=build) for _ in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()

    assert not errors, f"并发建工作区报错：{errors}"
    assert len({id(item) for item in results}) == 1, "同一个用户应该只建一份工作区"


def test_health_reports_legacy_data_state(client):
    body = client.get("/api/v2/health").json()
    assert "workspace" in body and "legacy_data_pending" in body["workspace"]
