from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from adapters.llm.deepseek import DeepSeekTutorResponder
from contracts.llm import LLMProviderError, TutorDelta, TutorEvidence, TutorRequest, TutorResponse


def _request() -> TutorRequest:
    return TutorRequest(
        course_name="高等数学",
        question="链式法则怎么用？",
        evidence=(
            TutorEvidence(
                citation_id="1",
                document="教材.md",
                page=12,
                chunk_id="chunk_1",
                content="复合函数求导时，先求外层导数，再乘以内层导数。",
            ),
        ),
    )


def _sse(*chunks: object) -> bytes:
    frames = [f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n" for chunk in chunks]
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _adapter(client: httpx.Client, *, max_retries: int = 0) -> DeepSeekTutorResponder:
    return DeepSeekTutorResponder(
        api_key="test-secret",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        max_output_tokens=2048,
        max_retries=max_retries,
        client=client,
    )


def test_streams_official_chat_completion_and_parses_deltas():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_sse(
                {"choices": [{"delta": {"role": "assistant"}}]},
                {"choices": [{"delta": {"content": "先求外层，"}}]},
                {"choices": [{"delta": {"content": "再乘内层。[1]"}}]},
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                },
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        items = list(adapter.respond(_request()))

        assert captured["url"] == "https://api.deepseek.com/chat/completions"
        assert captured["authorization"] == "Bearer test-secret"
        body = captured["body"]
        assert isinstance(body, dict)
        assert body["model"] == "deepseek-v4-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert body["max_tokens"] == 2048
        assert body["stream"] is True
        assert "<evidence>" in body["messages"][1]["content"]
        assert "教材.md" in body["messages"][1]["content"]

        deltas, result = items[:-1], items[-1]
        assert [item.text for item in deltas] == ["先求外层，", "再乘内层。[1]"]
        assert isinstance(result, TutorResponse)
        assert (result.text, result.mode, result.provider, result.model) == ("先求外层，再乘内层。[1]", "provider", "deepseek", "deepseek-v4-flash")
        assert result.finish_reason == "stop"
        assert result.usage["total_tokens"] == 120
        assert adapter.health()["last_call_ok"] is True


def test_empty_evidence_request_marks_the_gap_and_instructs_labeled_answer():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "通用回答"}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        empty = TutorRequest(course_name="LLM", question="你好", evidence=())
        items = list(_adapter(client).respond(empty))
    body = captured["body"]
    assert isinstance(body, dict)
    assert "（本轮未检索到相关教材内容）" in body["messages"][1]["content"]
    assert "以下不是当前教材结论" in body["messages"][0]["content"]
    assert items[-1].text == "通用回答"


def test_retries_before_first_delta_then_succeeds():
    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]}))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        items = list(_adapter(client, max_retries=1).respond(_request()))
    assert len(calls) == 2
    assert items[-1].text == "答案"


def test_mid_stream_drop_is_reported_as_stream_interrupted_not_retried():
    calls: list[int] = []

    def dropping_stream() -> Iterator[bytes]:
        yield b'data: {"choices":[{"delta":{"content":"\xe9\x93\xbe\xe5\xbc\x8f"}}]}\n\n'
        raise httpx.ReadError("connection lost")

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, content=dropping_stream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client, max_retries=2)
        stream = adapter.respond(_request())
        assert next(stream) == TutorDelta("链式")
        with pytest.raises(LLMProviderError) as raised:
            list(stream)
        assert raised.value.code == "stream_interrupted"
        assert len(calls) == 1  # 已输出 delta 后不得重放整轮
        assert adapter.health()["last_error_code"] == "stream_interrupted"


def test_provider_error_is_sanitized_and_recorded():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "sensitive-provider-body"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = _adapter(client)
        with pytest.raises(LLMProviderError) as raised:
            list(adapter.respond(_request()))
        assert raised.value.code == "http_401"
        assert raised.value.retryable is False
        assert "test-secret" not in str(raised.value)
        assert "sensitive-provider-body" not in str(raised.value)
        assert adapter.health()["last_error_code"] == "http_401"
