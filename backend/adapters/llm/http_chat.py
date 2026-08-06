from __future__ import annotations

import threading
from collections.abc import Mapping

import httpx


class HttpChatBase:
    """两条对话协议共用的部分：连接参数、extra_body 守卫、健康状态、错误说明的提取。

    端点后缀与协议字段由子类声明；请求体怎么拼、事件怎么解析各自实现。
    """

    # 拼在 base_url 后面的路径；以及 extra_body 里不许出现的字段名 → 拦它的理由。
    endpoint_path = ""
    protocol_fields: dict[str, str] = {}
    default_provider = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        provider: str = "",
        extra_body: Mapping[str, object] | None = None,
        connect_timeout_seconds: float = 10,
        total_timeout_seconds: float = 180,
        max_output_tokens: int = 8192,
        max_retries: int = 2,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key or not base_url or not model:
            raise ValueError("适配器需要 api_key、base_url 和 model")
        self._model = model
        self._provider = provider or self.default_provider
        self._extra_body = dict(extra_body or {})
        # 这些字段一旦从 extra_body 进来就会改坏行为，宁可启动就报错也不要留个难查的运行时故障。
        refused: dict[str, list[str]] = {}
        for name in sorted(self._extra_body.keys() & self.protocol_fields.keys()):
            refused.setdefault(self.protocol_fields[name], []).append(name)
        if refused:
            raise ValueError("；".join(f"extra_body 不能带 {'、'.join(names)}：{reason}"
                                       for reason, names in refused.items()))
        self._endpoint = f"{base_url.rstrip('/')}{self.endpoint_path}"
        self._api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._max_output_tokens = max(256, max_output_tokens)
        self._max_retries = max(0, max_retries)
        self._owns_client = client is None
        timeout = httpx.Timeout(total_timeout_seconds, connect=connect_timeout_seconds)
        self._client = client or httpx.Client(timeout=timeout)
        self._state_lock = threading.Lock()
        self._last_call_ok: bool | None = None
        self._last_error_code: str | None = None

    @property
    def mode(self) -> str:
        return "provider"

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model(self) -> str:
        return self._model

    def health(self) -> dict[str, object]:
        with self._state_lock:
            last_call_ok, last_error_code = self._last_call_ok, self._last_error_code
        return {
            "configured": True,
            "enabled": True,
            "mode": self.mode,
            "adapter_available": True,
            "provider": self.provider,
            "model": self.model,
            "last_call_ok": last_call_ok,
            "last_error_code": last_error_code,
        }

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _detail(self, response: httpx.Response) -> str:
        """带上服务端对错误的说明：4xx 只有状态码根本没法查——模型名错了、参数不被接受、
        还是缺了某个必传字段，全靠这句话。只取结构化的 error.message，不回显整个响应体；
        密钥就算被服务端回显也在这里抹掉。这条消息只进本地 trace，不随事件发给前端。"""
        try:
            response.read()
            body = response.json()
            # vLLM / FastAPI 系的错误说明在顶层 detail 或 message，不在 error.message 里。
            message = (body.get("error") or {}).get("message") or body.get("message") \
                or (body.get("detail") if isinstance(body.get("detail"), str) else None)
        except Exception:
            return ""
        return self._sanitize(message)

    def _sanitize(self, message: object) -> str:
        """服务端的说明压成一行、截断、抹掉密钥，供拼进错误消息。"""
        if not isinstance(message, str):
            return ""
        text = " ".join(message.split())[:200]
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return f"：{text}" if text else ""

    def _record_success(self) -> None:
        with self._state_lock:
            self._last_call_ok = True
            self._last_error_code = None

    def _record_failure(self, code: str) -> None:
        with self._state_lock:
            self._last_call_ok = False
            self._last_error_code = code
