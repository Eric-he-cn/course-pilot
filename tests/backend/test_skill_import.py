from __future__ import annotations

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
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


def _skill(*, name="exam_drill", tools="[search_materials, concept_search]") -> str:
    return (
        f"---\nname: {name}\ndescription: 按考纲抽查\nwhen_to_use: 用户要按考纲自测时\n"
        f"allowed_tools: {tools}\n---\n\n# 规程\n\n1. 先查教材证据。\n"
    )


def _upload(client, text: str, filename: str = "SKILL.md"):
    return client.post("/api/v2/skills", files={"file": (filename, text, "text/markdown")})


def test_imported_skill_is_disabled_until_enabled(client):
    created = _upload(client, _skill()).json()
    assert created["status"] == "draft"
    registry = workspace(client).skills
    assert "exam_drill" not in registry.names()  # 草稿不进系统提示，也不能被 use_skill 加载

    assert client.patch("/api/v2/skills/exam_drill", json={"enabled": True}).json()["status"] == "enabled"
    assert "exam_drill" in registry.names()
    assert "exam_drill" in registry.summaries()
    assert registry.get("exam_drill").allowed_tools == ("search_materials", "concept_search")


def test_privileged_tools_are_refused_instead_of_silently_dropped(client):
    """越权申请不静默降权：导入为 permission_denied，且不允许启用。"""
    response = _upload(client, _skill(tools="[search_materials, memory_patch, plan_update]"))
    body = response.json()
    assert body["status"] == "permission_denied"
    assert body["denied_tools"] == ["memory_patch", "plan_update"]

    blocked = client.patch("/api/v2/skills/exam_drill", json={"enabled": True})
    assert blocked.status_code == 409
    assert "memory_patch" in blocked.json()["error"]["message"]
    assert "exam_drill" not in workspace(client).skills.names()


def test_builtin_skills_cannot_be_shadowed(client):
    response = _upload(client, _skill(name="practice"))
    assert response.status_code == 422
    assert "同名" in response.json()["error"]["message"]
    # 内建 practice 仍然是代码里那一份
    assert workspace(client).skills.get("practice").origin == "builtin"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("没有 frontmatter 的正文", "frontmatter"),
        ("---\nname: x\ndescription: d\n---\n\n正文", "缺少"),
        (_skill(name="Bad Name"), "不合法"),
        (_skill(tools="[memory_patch]"), "没有一个是可授予的"),
        (_skill().split("---\n\n")[0] + "---\n\n", "正文为空"),
    ],
)
def test_invalid_uploads_are_rejected(client, text, expected):
    response = _upload(client, text)
    assert response.status_code == 422
    assert expected in response.json()["error"]["message"]


def test_only_markdown_is_accepted(client):
    assert _upload(client, _skill(), filename="skill.zip").status_code == 422


def test_delete_removes_an_enabled_skill_from_the_registry(client):
    _upload(client, _skill())
    client.patch("/api/v2/skills/exam_drill", json={"enabled": True})
    assert client.delete("/api/v2/skills/exam_drill").status_code == 204
    assert "exam_drill" not in workspace(client).skills.names()
    assert client.delete("/api/v2/skills/exam_drill").status_code == 404


def test_catalog_lists_builtin_and_imported(client):
    _upload(client, _skill())
    catalog = client.get("/api/v2/skills").json()
    origins = {item["name"]: item["origin"] for item in catalog["skills"]}
    assert origins["practice"] == "builtin" and origins["exam_drill"] == "user"
    assert "memory_patch" not in catalog["importable_tools"]
