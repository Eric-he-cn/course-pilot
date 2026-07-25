from __future__ import annotations

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from adapters.llm.qwen_ocr import QwenOcrTranscriber
from app.main import create_app
from contracts.llm import LLMProviderError, VisionTranscription
from modules.sessions.images import process_image

from test_core_api import _events, _settings


def _png(width: int = 64, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _adapter(client: httpx.Client, *, max_retries: int = 0) -> QwenOcrTranscriber:
    return QwenOcrTranscriber(
        api_key="test-secret",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-vl-ocr",
        max_retries=max_retries,
        client=client,
    )


def test_qwen_ocr_sends_data_url_and_parses_transcription():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "梯度下降\nw = w - lr * dL/dw"}}],
                "usage": {"prompt_tokens": 218, "completion_tokens": 23, "total_tokens": 241},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = _adapter(client).transcribe(content=b"fake-image", mime_type="image/png")

    assert captured["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen-vl-ocr"
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1]["text"] == "Read all the text in the image."
    assert isinstance(result, VisionTranscription)
    assert result.plain_text == "梯度下降\nw = w - lr * dL/dw"
    assert result.needs_confirmation is False
    assert result.usage["total_tokens"] == 241


def test_qwen_ocr_empty_transcription_needs_confirmation():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = _adapter(client).transcribe(content=b"x", mime_type="image/jpeg")
    assert result.plain_text == ""
    assert result.needs_confirmation is True


def test_qwen_ocr_error_is_sanitized():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "sensitive-provider-body"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as raised:
            adapter.transcribe(content=b"x", mime_type="image/png")
    assert raised.value.code == "http_401"
    assert "test-secret" not in str(raised.value)
    assert "sensitive-provider-body" not in str(raised.value)
    assert adapter.health()["last_error_code"] == "http_401"


def test_process_image_rejects_bad_mime_and_oversized_bytes():
    with pytest.raises(ValueError):
        process_image(content=_png(), mime_type="image/gif", max_bytes=10_000_000, max_pixels=12_000_000)
    with pytest.raises(ValueError):
        process_image(content=_png(), mime_type="image/png", max_bytes=10, max_pixels=12_000_000)
    with pytest.raises(ValueError):
        process_image(content=b"not-an-image", mime_type="image/png", max_bytes=10_000_000, max_pixels=12_000_000)


def test_process_image_rejects_decompression_bomb_before_decoding():
    """几百 KB 的图能解压成几亿像素；必须按 header 尺寸拒掉，而不是解码后才发现。"""
    buffer = io.BytesIO()
    Image.new("L", (20000, 20000), 255).save(buffer, format="PNG", optimize=True)
    bomb = buffer.getvalue()
    assert len(bomb) < 10_000_000  # 体积检查放不住它
    with pytest.raises(ValueError):
        process_image(content=bomb, mime_type="image/png", max_bytes=10_000_000, max_pixels=12_000_000)


def test_process_image_downscales_and_strips_exif():
    source = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0110] = "TestCamera"  # Model tag
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", exif=exif)

    processed = process_image(content=buffer.getvalue(), mime_type="image/jpeg", max_bytes=10_000_000, max_pixels=20_000)
    assert processed.width * processed.height <= 20_000
    assert processed.width / processed.height == pytest.approx(2.0, abs=0.05)
    assert not Image.open(io.BytesIO(processed.content)).getexif()


class _FakeVision:
    provider, model = "dashscope", "qwen-vl-ocr"

    def transcribe(self, *, content: bytes, mime_type: str) -> VisionTranscription:
        return VisionTranscription(plain_text="链式法则示例图", provider=self.provider, model=self.model, needs_confirmation=False)

    def health(self) -> dict[str, object]:
        return {"configured": True, "enabled": True, "provider": self.provider, "model": self.model}

    def close(self) -> None: ...


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _session(client: TestClient) -> str:
    return client.post("/api/v2/sessions", json={"scope_mode": "general"}).json()["id"]


def test_upload_attachment_returns_feature_disabled_without_vision_slot(client):
    response = client.post(f"/api/v2/sessions/{_session(client)}/attachments", files={"file": ("a.png", _png(), "image/png")})
    assert response.status_code == 409
    assert "feature_disabled" in response.text


def test_upload_attachment_transcribes_and_turn_injects_transcription(client):
    client.app.state.application.sessions._vision = _FakeVision()  # 测试注入：绕过真实网络调用
    course = client.post("/api/v2/courses", json={"name": "高等数学"}).json()
    session_id = _session(client)

    upload = client.post(f"/api/v2/sessions/{session_id}/attachments", files={"file": ("题目.png", _png(), "image/png")})
    assert upload.status_code == 201
    attachment = upload.json()
    assert attachment["transcription"] == "链式法则示例图"
    assert attachment["needs_confirmation"] is False

    response = client.post(
        f"/api/v2/sessions/{session_id}/turns",
        json={"client_request_id": "r1", "message": "高等数学 这道题怎么做？", "attachment_ids": [attachment["id"]]},
    )
    assert response.status_code == 200
    events = _events(response.text)
    assert events[-1][0] == "turn_completed"
    user_message = client.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"][0]
    assert "[图片转录：题目.png]" in user_message["content"]
    assert "链式法则示例图" in user_message["content"]
    assert course["id"]  # 课程创建成功即可，解析细节由 core api 测试覆盖


def test_turn_with_unknown_attachment_fails_cleanly(client):
    session_id = _session(client)
    response = client.post(
        f"/api/v2/sessions/{session_id}/turns",
        json={"client_request_id": "r1", "message": "看看这张图", "attachment_ids": ["attachment_missing"]},
    )
    events = _events(response.text)
    assert events == [("turn_failed", {"error_code": "attachment_not_found", "retryable": False})]


def test_attachment_of_another_session_is_rejected(client):
    client.app.state.application.sessions._vision = _FakeVision()
    session_a, session_b = _session(client), _session(client)
    attachment = client.post(f"/api/v2/sessions/{session_a}/attachments", files={"file": ("a.png", _png(), "image/png")}).json()
    response = client.post(
        f"/api/v2/sessions/{session_b}/turns",
        json={"client_request_id": "r1", "message": "看看这张图", "attachment_ids": [attachment["id"]]},
    )
    assert _events(response.text)[0][1]["error_code"] == "attachment_not_found"
