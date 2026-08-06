from __future__ import annotations

import time

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import Settings
from test_extract import _docx, _paragraph, _pptx


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "data" / "coursepilot.db", uploads_dir=tmp_path / "data" / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=60, chunk_overlap=10, top_k_results=6,
        material_max_bytes=10 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _poll_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        response = client.get(f"/api/v2/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"job {job_id} did not reach a terminal state")


_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _upload_and_index(client: TestClient, course_id: str, name: str, payload, content_type: str) -> dict[str, object]:
    upload = client.post(f"/api/v2/courses/{course_id}/materials",
                         files={"file": (name, payload, content_type)})
    assert upload.status_code == 201, upload.text
    material_id = upload.json()["id"]
    job = client.post(f"/api/v2/materials/{material_id}/index")
    assert job.status_code == 200, job.text
    assert _poll_job(client, job.json()["id"])["status"] == "completed"
    return next(row for row in client.get(f"/api/v2/courses/{course_id}/materials").json()
                if row["id"] == material_id)


@pytest.mark.parametrize("name,content_type,needle", [
    ("讲义.txt", "text/plain", "时间片轮转"),
    ("讲义.md", "text/markdown", "护航效应"),
    ("讲义.docx", _DOCX_TYPE, "抢占式调度"),
    ("讲义.pptx", _PPTX_TYPE, "上下文切换"),
])
def test_every_supported_format_indexes_and_becomes_searchable_over_http(client, tmp_path, name, content_type, needle):
    """非 PDF 的几种格式此前只在解析层的单测里走过，整条 HTTP 链路（上传 → 索引 → 检索）没人走。
    多部分表单的 content-type、落盘后缀、提取分支任何一处不对，用户看到的都是一份空教材。"""
    bodies = {
        "讲义.txt": lambda: f"{needle}把 CPU 按固定时长切给每个任务。".encode(),
        "讲义.md": lambda: f"# 调度\n\n{needle}是长作业拖住短作业造成的。\n".encode(),
        "讲义.docx": lambda: _docx(tmp_path, _paragraph(f"{needle}允许高优先级任务打断当前任务。")).read_bytes(),
        "讲义.pptx": lambda: _pptx(tmp_path, [f"{needle}会带来直接与间接开销。"]).read_bytes(),
    }
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()

    listed = _upload_and_index(client, course["id"], name, bodies[name](), content_type)

    assert listed["status"] == "indexed" and listed["chunk_count"] > 0
    hits = client.post(f"/api/v2/courses/{course['id']}/knowledge/search", json={"query": needle}).json()
    assert hits and hits[0]["material_name"] == name, hits


def test_an_unsupported_extension_is_refused_with_a_readable_reason(client):
    """挡在上传这一步，不能收下再在索引里失败——用户看到的是一份永远索引不成的教材。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()

    refused = client.post(f"/api/v2/courses/{course['id']}/materials",
                          files={"file": ("讲义.epub", b"whatever", "application/epub+zip")})

    assert refused.status_code == 422, refused.text
    assert client.get(f"/api/v2/courses/{course['id']}/materials").json() == []


def test_course_scoped_material_index_search_wiki_and_health(client):
    calculus = client.post("/api/v2/courses", json={"name": "高等数学"}).json()
    physics = client.post("/api/v2/courses", json={"name": "大学物理"}).json()
    upload = client.post(
        f"/api/v2/courses/{calculus['id']}/materials",
        files={"file": ("calculus.md", "# 链式法则\n\n先求外层导数，再乘内层导数。\n", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text
    material = upload.json()
    job = client.post(f"/api/v2/materials/{material['id']}/index")
    assert job.status_code == 200, job.text
    assert job.json()["status"] in {"queued", "running", "completed"}
    job = _poll_job(client, job.json()["id"])
    assert job["status"] == "completed"
    assert job["retrieval_backend"] == "sqlite_fts"

    results = client.post(f"/api/v2/courses/{calculus['id']}/knowledge/search", json={"query": "链式法则"})
    assert results.status_code == 200, results.text
    assert results.json()[0]["material_id"] == material["id"]
    assert client.post(f"/api/v2/courses/{physics['id']}/knowledge/search", json={"query": "链式法则"}).json() == []

    assert client.post(f"/api/v2/materials/{material['id']}/wiki").status_code == 409
    assert client.patch(f"/api/v2/courses/{calculus['id']}", json={"wiki_enabled": True}).status_code == 200
    wiki = client.post(f"/api/v2/materials/{material['id']}/wiki")
    assert wiki.status_code == 200
    assert _poll_job(client, wiki.json()["id"])["stage"] == "wiki_completed"

    health = client.get("/api/v2/health")
    assert health.status_code == 200
    assert health.json()["llm"]["mode"] == "demo_fallback"
    assert health.json()["llm"]["enabled"] is False
    assert "api_key" not in health.json()["llm"]


def test_the_wiki_report_is_readable_after_the_build_and_ignores_unfinished_ones(client):
    """覆盖率报告落在任务记录里。界面刷新后内存里没有任务记录，靠这个接口把那一行找回来。"""
    course = client.post("/api/v2/courses", json={"name": "线性代数"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("linalg.md", "# 特征值\n\n特征值是矩阵作用下方向不变的那个倍数。\n", "text/markdown")},
    ).json()
    _poll_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])

    assert client.get("/api/v2/materials/mat_nope/wiki/report").status_code == 404
    empty = client.get(f"/api/v2/materials/{material['id']}/wiki/report")
    assert empty.status_code == 200 and empty.json() == {"job": None}, empty.text

    assert client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True}).status_code == 200
    build = _poll_job(client, client.post(f"/api/v2/materials/{material['id']}/wiki").json()["id"])
    assert build["status"] == "completed", build

    report = client.get(f"/api/v2/materials/{material['id']}/wiki/report")
    assert report.status_code == 200, report.text
    assert report.json()["job"]["id"] == build["id"]
    assert str(report.json()["job"]["error"]).startswith("wiki_coverage "), report.text

    # 又发起一次、还没跑完：报的仍是上次那份，半截任务没有报告可给。
    workspace(client).knowledge._repository.create_job(
        type="wiki", material_id=material["id"], course_id=course["id"])
    pending = client.get(f"/api/v2/materials/{material['id']}/wiki/report").json()["job"]
    assert pending["id"] == build["id"] and pending["status"] == "completed"


def test_the_wiki_report_follows_the_newest_finished_build_and_skips_failed_ones(client):
    """重建过就该报新的那份。排序反了会一直停在第一次构建的数字上，界面看不出来。"""
    course = client.post("/api/v2/courses", json={"name": "概率论"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("prob.md", "# 全概率公式\n\n把样本空间划分成互斥事件再加权求和。\n", "text/markdown")},
    ).json()
    _poll_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})

    first = _poll_job(client, client.post(f"/api/v2/materials/{material['id']}/wiki").json()["id"])
    second = _poll_job(client, client.post(f"/api/v2/materials/{material['id']}/wiki").json()["id"])
    assert first["id"] != second["id"] and second["status"] == "completed"
    assert client.get(f"/api/v2/materials/{material['id']}/wiki/report").json()["job"]["id"] == second["id"]

    # 之后失败的一次不能顶替上一次成功的报告——它的 error 是报错文字，不是覆盖率。
    repository = workspace(client).knowledge._repository
    broken = repository.create_job(type="wiki", material_id=material["id"], course_id=course["id"])
    repository.update_job(broken.id, status="failed", stage="failed", progress=100, error_message="构建失败")
    latest = client.get(f"/api/v2/materials/{material['id']}/wiki/report").json()["job"]
    assert latest["id"] == second["id"]
    assert str(latest["error"]).startswith("wiki_coverage ")
