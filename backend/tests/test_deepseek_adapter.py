from __future__ import annotations

import json
import unittest
from collections.abc import Iterator

import httpx

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


class DeepSeekAdapterTests(unittest.TestCase):
    def test_streams_official_chat_completion_and_parses_deltas(self) -> None:
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

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = _adapter(client)
        items = list(adapter.respond(_request()))

        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer test-secret")
        body = captured["body"]
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 2048)
        self.assertTrue(body["stream"])
        self.assertIn("<evidence>", body["messages"][1]["content"])
        self.assertIn("教材.md", body["messages"][1]["content"])

        deltas, result = items[:-1], items[-1]
        self.assertEqual([item.text for item in deltas], ["先求外层，", "再乘内层。[1]"])
        self.assertIsInstance(result, TutorResponse)
        assert isinstance(result, TutorResponse)
        self.assertEqual((result.text, result.mode, result.provider, result.model), ("先求外层，再乘内层。[1]", "provider", "deepseek", "deepseek-v4-flash"))
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["total_tokens"], 120)
        self.assertEqual(adapter.health()["last_call_ok"], True)
        client.close()

    def test_retries_before_first_delta_then_succeeds(self) -> None:
        calls: list[int] = []

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, content=_sse({"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]}))

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = _adapter(client, max_retries=1)
        items = list(adapter.respond(_request()))
        self.assertEqual(len(calls), 2)
        self.assertEqual(items[-1].text, "答案")
        client.close()

    def test_mid_stream_drop_is_reported_as_stream_interrupted_not_retried(self) -> None:
        calls: list[int] = []

        def dropping_stream() -> Iterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"\xe9\x93\xbe\xe5\xbc\x8f"}}]}\n\n'
            raise httpx.ReadError("connection lost")

        def handler(_request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, content=dropping_stream())

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = _adapter(client, max_retries=2)
        stream = adapter.respond(_request())
        first = next(stream)
        self.assertEqual(first, TutorDelta("链式"))
        with self.assertRaises(LLMProviderError) as raised:
            list(stream)
        self.assertEqual(raised.exception.code, "stream_interrupted")
        self.assertEqual(len(calls), 1)  # 已输出 delta 后不得重放整轮
        self.assertEqual(adapter.health()["last_error_code"], "stream_interrupted")
        client.close()

    def test_provider_error_is_sanitized_and_recorded(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "sensitive-provider-body"}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = _adapter(client)
        with self.assertRaises(LLMProviderError) as raised:
            list(adapter.respond(_request()))
        self.assertEqual(raised.exception.code, "http_401")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertNotIn("sensitive-provider-body", str(raised.exception))
        self.assertEqual(adapter.health()["last_error_code"], "http_401")
        client.close()


if __name__ == "__main__":
    unittest.main()
