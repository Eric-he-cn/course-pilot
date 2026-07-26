from __future__ import annotations

import json
import time
import uuid

import httpx


BASE_URL = "http://127.0.0.1:8000/api/v2"


def require(response: httpx.Response) -> dict | list:
    response.raise_for_status()
    return response.json()


def wait_for_job(client: httpx.Client, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = require(client.get(f"{BASE_URL}/jobs/{job_id}"))
        if isinstance(job, dict) and job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.05)
    raise RuntimeError(f"job {job_id} did not finish")


def main() -> None:
    suffix = uuid.uuid4().hex[:8]
    course_name = f"高等数学 Smoke {suffix}"
    with httpx.Client(timeout=90) as client:
        health = require(client.get(f"{BASE_URL}/health"))
        if health["llm"]["mode"] != "provider":
            raise RuntimeError(f"远端适配器未启用，当前走的是本地兜底：{health['llm']}")
        course = require(client.post(f"{BASE_URL}/courses", json={"name": course_name}))
        material = require(
            client.post(
                f"{BASE_URL}/courses/{course['id']}/materials",
                files={"file": ("smoke.md", "链式法则：先求外层导数，再乘内层导数。", "text/markdown")},
            )
        )
        queued = require(client.post(f"{BASE_URL}/materials/{material['id']}/index"))
        job = wait_for_job(client, queued["id"])
        if job["status"] != "completed":
            raise RuntimeError(f"index failed: {job.get('error')}")

        session = require(client.post(f"{BASE_URL}/sessions", json={"scope_mode": "general"}))
        turn = client.post(
            f"{BASE_URL}/sessions/{session['id']}/turns",
            json={"client_request_id": f"smoke-{suffix}", "message": f"{course_name} 的链式法则怎么用？"},
        )
        turn.raise_for_status()
        if "event: course_resolution" not in turn.text or '"status": "resolved"' not in turn.text:
            raise RuntimeError("course resolution did not resolve")
        if "event: tool_call" not in turn.text:
            raise RuntimeError("resolved turn did not emit a tool_call span")
        if "event: citation" not in turn.text:
            raise RuntimeError("resolved turn returned no citation")
        if '"responder_mode": "provider"' not in turn.text:
            raise RuntimeError("这一轮没有走远端模型")
        messages = require(client.get(f"{BASE_URL}/sessions/{session['id']}/messages"))
        if not isinstance(messages, dict) or len(messages["messages"]) != 2:
            raise RuntimeError("messages were not persisted")
        expected_title = f"{course_name} 的链式法则怎么用？"[:30]
        if messages["session"]["title"] != expected_title:
            raise RuntimeError("会话标题未由首条消息生成——8000 端口上可能运行着旧代码的后端进程")

        print(
            json.dumps(
                {
                    "ok": True,
                    "llm_mode": health["llm"]["mode"],
                    "course_id": course["id"],
                    "job_status": job["status"],
                    "sse": ["turn_started", "course_resolution", "tool_call", "citation", "tool_result", "text_delta", "turn_completed"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
