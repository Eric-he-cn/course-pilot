from __future__ import annotations

import json
import unittest

import httpx

from adapters.llm.deepseek import DeepSeekTutorResponder
from contracts.llm import LLMProviderError, TutorEvidence, TutorRequest


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


class DeepSeekAdapterTests(unittest.TestCase):
    def test_builds_official_chat_completion_request_and_parses_response(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("Authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "先求外层，再乘内层。[1]"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = DeepSeekTutorResponder(
            api_key="test-secret",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            max_output_tokens=2048,
            max_retries=0,
            client=client,
        )
        result = adapter.respond(_request())

        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["authorization"], "Bearer test-secret")
        body = captured["body"]
        self.assertIsInstance(body, dict)
        assert isinstance(body, dict)
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["max_tokens"], 2048)
        self.assertFalse(body["stream"])
        self.assertIn("<evidence>", body["messages"][1]["content"])
        self.assertIn("教材.md", body["messages"][1]["content"])
        self.assertEqual((result.text, result.mode, result.provider, result.model), ("先求外层，再乘内层。[1]", "provider", "deepseek", "deepseek-v4-flash"))
        self.assertEqual(result.usage["total_tokens"], 120)
        self.assertEqual(adapter.health()["last_call_ok"], True)
        client.close()

    def test_provider_error_is_sanitized_and_recorded(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "sensitive-provider-body"}})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        adapter = DeepSeekTutorResponder(
            api_key="test-secret",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
            max_retries=0,
            client=client,
        )
        with self.assertRaises(LLMProviderError) as raised:
            adapter.respond(_request())
        self.assertEqual(raised.exception.code, "http_401")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("test-secret", str(raised.exception))
        self.assertNotIn("sensitive-provider-body", str(raised.exception))
        self.assertEqual(adapter.health()["last_error_code"], "http_401")
        client.close()


if __name__ == "__main__":
    unittest.main()
