from __future__ import annotations

import io
import zipfile

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
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


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in entries.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def _upload_zip(client, entries: dict[str, str]):
    return client.post("/api/v2/skills", files={"file": ("skill.zip", _zip(entries), "application/zip")})


def test_zip_bundle_merges_reference_files_into_the_procedure(client):
    """多文件 skill 的附带资料要真进正文，否则规程里的指路指向空气。"""
    created = _upload_zip(client, {
        "exam_drill/SKILL.md": _skill(),
        "exam_drill/references/rubric.md": "# 评分档位\n\nA 档：证据完整。",
    })
    assert created.status_code == 201
    body = workspace(client).skills._user_skills.get("exam_drill").body
    assert "评分档位" in body
    assert "references/rubric.md" in body  # 标出出处，模型才对得上规程里写的路径


def test_zip_without_a_skill_md_is_rejected(client):
    response = _upload_zip(client, {"exam_drill/README.md": "# 说明"})
    assert response.status_code == 422
    assert "SKILL.md" in response.json()["error"]["message"]


def test_executables_are_skipped_and_reported(client):
    """这里没有 shell，脚本收了也跑不了——跳过并说出来，别让用户以为整份都生效了。"""
    body = _upload_zip(client, {
        "SKILL.md": _skill(), "scripts/grade.py": "print(1)", "assets/logo.png": "\x00\x01",
    }).json()
    assert body["skipped_files"] == ["assets/logo.png", "scripts/grade.py"]
    assert "grade.py" not in workspace(client).skills._user_skills.get("exam_drill").body


def test_zip_paths_escaping_the_bundle_are_dropped(client):
    body = _upload_zip(client, {"SKILL.md": _skill(), "../../etc/passwd.md": "root:x:0:0"}).json()
    assert body["status"] == "draft"
    assert "root:x" not in workspace(client).skills._user_skills.get("exam_drill").body


def test_folder_upload_sends_files_with_relative_paths(client):
    """浏览器选目录时逐个文件上传，相对路径写在文件名里。"""
    response = client.post("/api/v2/skills", files=[
        ("file", ("exam_drill/SKILL.md", _skill(), "text/markdown")),
        ("file", ("exam_drill/references/notes.md", "# 补充\n\n随堂笔记要点。", "text/markdown")),
    ])
    assert response.status_code == 201
    assert "随堂笔记要点" in workspace(client).skills._user_skills.get("exam_drill").body


def test_broken_archive_is_rejected(client):
    response = client.post("/api/v2/skills", files={"file": ("skill.zip", b"not a zip", "application/zip")})
    assert response.status_code == 422
    assert "ZIP" in response.json()["error"]["message"]


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
