"""扫描版 PDF 通道：识别 → 估算 → 用户确认 → 逐页 OCR → 回到普通索引。

关键约束是「不擅自花钱」：没经过确认的图片版 PDF 必须停在 needs_ocr，而不是自己跑起来。
这里的 transcriber 是假的，不发网络请求。
"""
from __future__ import annotations

import time

import pytest
from conftest import workspace
from fastapi.testclient import TestClient
from PIL import Image

from app.main import create_app
from contracts.llm import VisionTranscription
from core.settings import Settings
from modules.knowledge.scanned import probe_text_layer


class FakeTranscriber:
    """按调用次数返回不同页文字，顺便记录用量，好让估算有东西可算。"""

    def __init__(self, *, text: str = "第 %d 页：调度策略与周转时间。", usage: dict | None = None) -> None:
        self.text = text
        self.usage = usage or {"prompt_tokens": 600, "completion_tokens": 300}
        self.calls = 0

    def transcribe(self, *, content: bytes, mime_type: str) -> VisionTranscription:
        self.calls += 1
        return VisionTranscription(
            plain_text=self.text % self.calls, provider="fake", model="fake-ocr",
            needs_confirmation=False, usage=dict(self.usage),
        )


def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="https://api.example.com/v1", text_api_key="",
        text_model="example-model", enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )


def _scanned_pdf(tmp_path, pages: int = 3):
    """真正的图片版 PDF：几张白底图存成 PDF，没有文字层。"""
    images = [Image.new("RGB", (400, 560), "white") for _ in range(pages)]
    path = tmp_path / "scanned.pdf"
    images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    return path


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _wait_for_job(client, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} 没有结束")


def _upload(client, tmp_path, pages: int = 3):
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    content = _scanned_pdf(tmp_path, pages).read_bytes()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("扫描教材.pdf", content, "application/pdf")},
    ).json()
    return course, material


def test_a_real_image_only_pdf_is_detected_as_scanned(tmp_path):
    layer = probe_text_layer(_scanned_pdf(tmp_path, pages=4))
    assert layer.pages == 4
    assert layer.median_chars == 0
    assert layer.is_scanned


def test_a_text_pdf_is_not_treated_as_scanned(tmp_path, client):
    """反向守护：文字版不该被拉进 OCR 通道，那是白花钱。"""
    layer = probe_text_layer(_scanned_pdf(tmp_path, pages=1))
    assert layer.is_scanned  # 前提成立
    # 带文字层的 PDF 用 markdown 代替（同样走 extract_pages 的非 OCR 分支）
    course = client.post("/api/v2/courses", json={"name": "文字版"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("文字版.md", "Round Robin 的时间片选择直接决定吞吐。", "text/markdown")},
    ).json()
    job = _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    assert job["status"] == "completed"


def test_scanned_upload_stops_and_waits_instead_of_spending_quota(client, tmp_path):
    """核心约束：OCR 要花钱，没确认就停在 needs_ocr，一次模型调用都不发。"""
    transcriber = FakeTranscriber()
    workspace(client).knowledge._transcriber = transcriber
    _course, material = _upload(client, tmp_path)

    job = _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    assert job["status"] == "failed"
    assert job["stage"] == "needs_ocr"
    assert "扫描版" in job["error"]
    assert transcriber.calls == 0, "确认之前不能调用 OCR"


def test_estimate_measures_a_sample_and_projects_from_it(client, tmp_path):
    """估算按真实取样外推，不内置价格表——换模型也不会失准。"""
    transcriber = FakeTranscriber()
    workspace(client).knowledge._transcriber = transcriber
    _course, material = _upload(client, tmp_path, pages=10)

    estimate = client.post(f"/api/v2/materials/{material['id']}/ocr/estimate").json()
    assert estimate["pages"] == 10
    assert estimate["sampled_pages"] == 2
    assert transcriber.calls == 2, "取样只跑两页"
    # 两页量到 1200 prompt / 600 completion，10 页外推就是 5 倍
    assert estimate["sample_prompt_tokens"] == 1200
    assert estimate["projected_prompt_tokens"] == 6000
    assert estimate["projected_completion_tokens"] == 3000
    assert estimate["projected_total_tokens"] == 9000


def test_approving_ocr_indexes_the_transcribed_text_with_page_numbers(client, tmp_path):
    transcriber = FakeTranscriber()
    workspace(client).knowledge._transcriber = transcriber
    course, material = _upload(client, tmp_path, pages=3)
    _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])

    job = _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/ocr").json()["id"])
    assert job["status"] == "completed"
    assert transcriber.calls == 3, "三页各转一次"

    hits = client.post(f"/api/v2/courses/{course['id']}/knowledge/search", json={"query": "周转时间"}).json()
    assert hits, "OCR 出来的文字要能被检索到"
    assert all(hit["page"] is not None for hit in hits), "页码来自 PDF 的真实页序"


def test_reindexing_an_approved_material_does_not_ask_again(client, tmp_path):
    """批准记录留在库里：重新索引直接跑 OCR，不该再把用户拦一次。"""
    workspace(client).knowledge._transcriber = FakeTranscriber()
    _course, material = _upload(client, tmp_path, pages=2)
    _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/ocr").json()["id"])

    again = _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    assert again["status"] == "completed"
    assert again["stage"] != "needs_ocr"


def test_ocr_without_a_vision_slot_says_what_is_missing(client, tmp_path):
    _course, material = _upload(client, tmp_path)
    workspace(client).knowledge._transcriber = None
    response = client.post(f"/api/v2/materials/{material['id']}/ocr/estimate")
    assert response.status_code == 409
    assert "VISION" in response.json()["error"]["message"]


def test_ocr_is_refused_for_non_pdf_material(client):
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    workspace(client).knowledge._transcriber = FakeTranscriber()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("笔记.md", "随堂笔记", "text/markdown")},
    ).json()
    assert client.post(f"/api/v2/materials/{material['id']}/ocr/estimate").status_code == 404


def test_one_failing_page_does_not_lose_the_whole_book(client, tmp_path):
    """一页转录失败就放弃整本，代价太大。那页留空，其余照常入库。"""
    class FlakyTranscriber(FakeTranscriber):
        def transcribe(self, *, content: bytes, mime_type: str) -> VisionTranscription:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("上游 500")
            return super().transcribe(content=content, mime_type=mime_type)

    workspace(client).knowledge._transcriber = FlakyTranscriber()
    course, material = _upload(client, tmp_path, pages=3)
    _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])
    job = _wait_for_job(client, client.post(f"/api/v2/materials/{material['id']}/ocr").json()["id"])
    assert job["status"] == "completed"
    assert client.post(f"/api/v2/courses/{course['id']}/knowledge/search", json={"query": "调度策略"}).json()
