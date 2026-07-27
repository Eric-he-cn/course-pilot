from __future__ import annotations

import base64
import threading
import time

import httpx

from contracts.llm import LLMProviderError, VisionTranscription

# 专用 OCR 模型往往只认这句约定提示词，换成自定义中文提示词可能返回坐标而不是文字。
_OCR_PROMPT = "Read all the text in the image."

# 拍照提问走的是通用多模态模型，任务不是抄字而是看懂这一页。同一张教材照片实测：
#   qwen3.5-ocr   「A B C / Figure 7.6: SJSF Again」——只抄字，还把 SJF 抄成 SJSF
#   qwen-vl-ocr   「关于响应时间的两个示例」——泛泛
#   qwen3-vl-plus 「两种调度策略下三个任务的时序执行图」——看懂了结构
#   qwen3.7-flash 「教材《Three Easy Pieces》关于调度的一页，用甘特图对比 SJF…」——最好
_UNDERSTAND_PROMPT = (
    "看这张图，用中文把它讲清楚，供后续问答使用：\n"
    "1. 图上的文字全部照录，公式用 LaTeX 写（行内 $...$）。\n"
    "2. 如果是题目，把题干、条件、选项完整写出来，不要作答。\n"
    "3. 如果有图表、示意图、代码，说明它在表达什么、各部分的关系。\n"
    "4. 只描述图里真实存在的内容，看不清的地方直接说看不清，不要补测。"
)


class VisionOcrTranscriber:
    """OpenAI 兼容的图片转录适配器（vision 槽位）：标准 image_url + base64。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        provider: str = "openai_compatible",
        # 纯抄字（扫描版逐页）用 OCR 提示词；拍照提问要模型看懂整页，换另一条。
        understand: bool = False,
        connect_timeout_seconds: float = 10,
        total_timeout_seconds: float = 180,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not base_url or not model:
            raise ValueError("适配器需要 api_key、base_url 和 model")
        self._model = model
        self._prompt = _UNDERSTAND_PROMPT if understand else _OCR_PROMPT
        self._provider = provider or "openai_compatible"
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._max_retries = max(0, max_retries)
        self._owns_client = client is None
        timeout = httpx.Timeout(total_timeout_seconds, connect=connect_timeout_seconds)
        self._client = client or httpx.Client(timeout=timeout)
        self._state_lock = threading.Lock()
        self._last_call_ok: bool | None = None
        self._last_error_code: str | None = None

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def transcribe(self, *, content: bytes, mime_type: str) -> VisionTranscription:
        image = base64.b64encode(content).decode("ascii")
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image}"}},
                        {"type": "text", "text": self._prompt},
                    ],
                }
            ],
        }
        attempt = 0
        while True:
            try:
                response = self._client.post(self._endpoint, json=payload, headers=self._headers)
                response.raise_for_status()
                data = response.json()
                text = str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                usage = {k: v for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
                self._record_success()
                return VisionTranscription(
                    plain_text=text,
                    provider=self.provider,
                    model=self.model,
                    # 空转录视为不可信，按架构 §5.7 交给用户确认，不直接进入讲解。
                    needs_confirmation=not text,
                    usage=usage,
                )
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                retryable = status == 429 or status >= 500
                if retryable and attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                code = f"http_{status}"
                self._record_failure(code)
                raise LLMProviderError(code, f"{self._provider} 图片转录失败（HTTP {status}）", retryable=retryable) from error
            except httpx.RequestError as error:
                if attempt < self._max_retries:
                    attempt += 1
                    time.sleep(0.2 * (2 ** (attempt - 1)))
                    continue
                self._record_failure("network_error")
                raise LLMProviderError("network_error", f"暂时无法连接 {self._provider}", retryable=True) from error
            except ValueError as error:
                self._record_failure("invalid_response")
                raise LLMProviderError("invalid_response", f"{self._provider} 返回了无法解析的响应", retryable=False) from error

    def health(self) -> dict[str, object]:
        with self._state_lock:
            last_call_ok, last_error_code = self._last_call_ok, self._last_error_code
        return {
            "configured": True,
            "enabled": True,
            "adapter_available": True,
            "provider": self.provider,
            "model": self.model,
            "last_call_ok": last_call_ok,
            "last_error_code": last_error_code,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _record_success(self) -> None:
        with self._state_lock:
            self._last_call_ok = True
            self._last_error_code = None

    def _record_failure(self, code: str) -> None:
        with self._state_lock:
            self._last_call_ok = False
            self._last_error_code = code
