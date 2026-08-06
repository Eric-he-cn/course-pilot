"""多教材 Wiki e2e 评测的共用件：HTTP 客户端、SSE 解析、文本归一化。

归一化口径与 e2e_wiki_dataset.yaml 头部的约定逐条对应，改这里等于改判据，别单独改一边。
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── 文本归一化 ────────────────────────────────────────────────────────────────

# NFKC 不折叠 CJK Radicals Supplement（U+2E80–U+2EFF），d2l 与 happy-llm 切片的文字层
# 正是这一段。表来自题目集头部，是扫全部 4 份切片得出的全集。
RADICAL_FOLD = str.maketrans({
    "⻓": "长", "⻆": "角", "⻅": "见", "⻔": "门",
    "⻛": "风", "⻜": "飞", "⻢": "马", "⻨": "麦",
    "⻉": "贝", "⻬": "齐", "⺠": "民", "⻚": "页",
})

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_MATH_BLOCK = re.compile(r"\$\$.*?\$\$", re.S)
_MATH_BRACKET = re.compile(r"\\\[.*?\\\]", re.S)
_MATH_PAREN = re.compile(r"\\\(.*?\\\)", re.S)
_MATH_INLINE = re.compile(r"\$[^$\n]*\$")


def strip_math_and_code(text: str) -> str:
    """剔除 LaTeX 数学环境与代码块。

    占比类与关键词类判据都不该把公式里的符号算成正文——`softmax`、`\\lambda` 这类
    在公式里必然出现，留着会让「回答里提到了 X」变成恒真。
    多字符定界符先删，否则 `$...$` 会把 `$$...$$` 拦腰截断。
    """
    for pattern in (_FENCE, _MATH_BLOCK, _MATH_BRACKET, _MATH_PAREN, _MATH_INLINE, _INLINE_CODE):
        text = pattern.sub(" ", text)
    return text


def normalize(text: str) -> str:
    """判据口径：NFKC → 部首折叠 → 剔公式与代码 → 连续空白压成一个空格。"""
    folded = unicodedata.normalize("NFKC", text or "").translate(RADICAL_FOLD)
    return re.sub(r"\s+", " ", strip_math_and_code(folded)).strip()


def squash(text: str) -> str:
    """conflate_pairs 的比对口径：归一化后去掉所有空白。

    PDF 文字层会把公式沿行拆开（`λ` 与 `2m` 分在两行），不去空白匹配不上。
    这里不剔公式：a / b 本身常常就是记号（`dZ[l]=dA[l]`），剔掉就什么都不剩。
    """
    folded = unicodedata.normalize("NFKC", text or "").translate(RADICAL_FOLD)
    return re.sub(r"\s+", "", folded)


# ── HTTP ─────────────────────────────────────────────────────────────────────

USER_HEADER = "X-CoursePilot-User"


class HttpError(RuntimeError):
    def __init__(self, code: int, body: str, path: str) -> None:
        super().__init__(f"HTTP {code} {path}: {body[:400]}")
        self.code, self.body, self.path = code, body, path


class Client:
    """后端 HTTP 客户端。用户身份走请求头，没有真正的登录动作（见 core/identity.py）。"""

    def __init__(self, base: str, user: str = "local", timeout: int = 900) -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.timeout = timeout

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {USER_HEADER: self.user}
        headers.update(extra or {})
        return headers

    def call(self, path: str, payload: Any | None = None, method: str | None = None,
             timeout: int | None = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}/api/v2{path}", data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=self._headers({"Content-Type": "application/json"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as error:
            raise HttpError(error.code, error.read().decode(errors="replace"), path) from error
        return json.loads(body) if body.strip() else {}

    def upload(self, course_id: str, path: Path) -> dict:
        boundary = "----coursepilot-wiki-eval"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            b"Content-Type: application/pdf\r\n\r\n",
            path.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode(),
        ])
        request = urllib.request.Request(
            f"{self.base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            raise HttpError(error.code, error.read().decode(errors="replace"),
                            f"/courses/{course_id}/materials") from error

    def turn(self, session_id: str, message: str, tag: str) -> str:
        """发一轮，返回原始 SSE 文本。解析失败时上层要能把原文 dump 出来。"""
        payload = json.dumps({"message": message, "client_request_id": tag}, ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{self.base}/api/v2/sessions/{session_id}/turns", data=payload, method="POST",
            headers=self._headers({"Content-Type": "application/json"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read().decode()
        except urllib.error.HTTPError as error:
            raise HttpError(error.code, error.read().decode(errors="replace"),
                            f"/sessions/{session_id}/turns") from error

    def wait_job(self, job_id: str, *, timeout: int = 3600, poll: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.call(f"/jobs/{job_id}")
            if job.get("status") in {"completed", "failed"}:
                return job
            time.sleep(poll)
        return {"status": "timeout", "error": f"{timeout}s 内没进终态", "id": job_id}


# ── SSE ──────────────────────────────────────────────────────────────────────

SEED_ORIGIN = "seed"


class Turn:
    """一轮对话的结构化结果。bad_frames 非空表示有帧没解析出来，上层要 dump 原文。"""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.events: list[tuple[str, dict]] = []
        self.bad_frames: list[str] = []
        for frame in raw.split("\n\n"):
            lines = [line for line in frame.splitlines() if line.strip()]
            if not lines:
                continue
            if len(lines) < 2 or not lines[0].startswith("event: "):
                self.bad_frames.append(frame[:400])
                continue
            try:
                self.events.append((lines[0].removeprefix("event: "),
                                    json.loads(lines[1].removeprefix("data: "))))
            except json.JSONDecodeError as error:
                self.bad_frames.append(f"{error}: {frame[:400]}")

    def named(self, name: str) -> list[dict]:
        return [data for event, data in self.events if event == name]

    @property
    def answer(self) -> str:
        return "".join(item.get("text", "") for item in self.named("text_delta"))

    @property
    def finish_reason(self) -> str:
        completed = self.named("turn_completed")
        return completed[-1].get("finish_reason", "") if completed else ""

    @property
    def failed(self) -> dict | None:
        items = self.named("turn_failed")
        return items[-1] if items else None

    def tool_calls(self) -> list[dict]:
        return [{"name": item.get("name"), "origin": item.get("origin"), "call_id": item.get("call_id")}
                for item in self.named("tool_call")]

    def citations(self) -> list[dict]:
        """全部登记过的引用，标出它是种子检索那次登记的还是模型自己检索出来的。

        落库的 citations 只留正文里标了编号的那些（cited_only），这里两种都要：
        「检索到了但没标」与「压根没检索到」是两件事，混成一个数就分不出来。
        """
        marks = {int(number) for number in re.findall(r"\[(\d+)\]", self.answer)}
        picked: list[dict] = []
        origin = None
        for name, data in self.events:
            if name == "tool_call":
                origin = data.get("origin")
            elif name == "citation":
                picked.append({
                    "number": data.get("number"),
                    "kind": data.get("kind"),
                    "document": data.get("document"),
                    "page": data.get("page"),
                    "chunk_id": data.get("chunk_id"),
                    "concept_id": data.get("concept_id"),
                    "concept_name": data.get("concept_name"),
                    "url": data.get("url"),
                    "score": data.get("score"),
                    # 知识页那条引用底下挂着它转述时依据的教材页，probe 靠这个判来源分布
                    "source_documents": sorted({item.get("document") for item in (data.get("sources") or [])
                                                if item.get("document")}),
                    "source_pages": data.get("source_pages"),
                    "origin": origin,
                    "cited": data.get("number") in marks,
                })
        return picked

    def context_usage(self) -> dict | None:
        """最后一次上报的上下文构成。没有这个事件就是 None，不补零。"""
        items = self.named("context_usage")
        return items[-1] if items else None

    def usage(self) -> dict | None:
        completed = self.named("turn_completed")
        return completed[-1].get("usage") if completed else None
