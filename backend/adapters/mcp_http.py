"""Streamable HTTP 传输：按 MCP 协议自己发 JSON-RPC，不引官方 SDK（依赖太重）。

三件事在这一层做完，上层拿到的就已经是安全的：地址过 SSRF 校验（连的是校验过的 IP，
域名只用于 Host 与 SNI）、响应体有字节上限、整条链路有超时。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from contracts.mcp import McpCallOutcome, McpHandshake, McpToolDescriptor, McpTransportError
from core.netguard import BlockedAddress, resolved_public_ips

logger = logging.getLogger(__name__)

# 我们按哪一版协议说话。server 报别的版本也照常继续：它只影响可选特性，
# 而我们只用 initialize / tools/list / tools/call 这三件最基础的。
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "coursepilot"
CLIENT_VERSION = "2.0"

# 响应体的读取上限。外部 server 可以回几十兆，读进内存就够把进程压垮；
# 超限直接判失败而不是截着解析——半截 JSON 解不出来，报「返回过大」才说得清。
RESPONSE_MAX_BYTES = 256 * 1024
# 交给模型的正文上限。截断要说出来（见 service 里拼的说明）。
TEXT_MAX_CHARS = 8_000
# 单个工具描述的上限：它进 schema、每轮都发，外部 server 写多长我们不能不管。
DESCRIPTION_MAX_CHARS = 600

_ACCEPT = "application/json, text/event-stream"


def _stringify(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class StreamableHttpTransport:
    """一个实例服务所有 server：每次调用自带 url 与凭据，实例本身不持有任何一台的配置。"""

    kind = "streamable_http"

    def __init__(
        self, *, connect_timeout_seconds: float = 10, total_timeout_seconds: float = 30,
        allow_loopback: bool = False, client: httpx.Client | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(total_timeout_seconds, connect=connect_timeout_seconds)
        self._allow_loopback = allow_loopback
        self._client = client

    def _http(self) -> httpx.Client:
        if self._client is None:
            # 不跟随重定向：跳到哪里由我们重新校验，不让 server 把请求引到别处。
            self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try: self._client.close()
            except Exception: pass

    # ---- 对外的两个动作 ----

    def handshake(self, *, url: str, credential: str = "") -> McpHandshake:
        target = self._target(url)
        session, info = self._initialize(target, credential)
        self._notify_initialized(target, credential, session)
        listed = self._request(target, credential, session, "tools/list", {})
        tools = []
        for item in listed.get("tools") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            schema = item.get("inputSchema")
            tools.append(McpToolDescriptor(
                name=name,
                description=_stringify(item.get("description") or "", DESCRIPTION_MAX_CHARS),
                input_schema=schema if isinstance(schema, dict) else {"type": "object", "properties": {}},
            ))
        return McpHandshake(
            protocol_version=str(info.get("protocolVersion") or ""),
            server_name=_stringify((info.get("serverInfo") or {}).get("name") or "", 120),
            server_version=_stringify((info.get("serverInfo") or {}).get("version") or "", 40),
            tools=tuple(tools),
        )

    def call(self, *, url: str, credential: str, tool: str, arguments: dict) -> McpCallOutcome:
        target = self._target(url)
        session, _ = self._initialize(target, credential)
        self._notify_initialized(target, credential, session)
        payload = self._request(target, credential, session, "tools/call",
                                {"name": tool, "arguments": arguments})
        parts = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                # 图片、音频、资源链接这一版都不解析，只说明有这么一块，免得模型以为内容没了。
                parts.append(f"（这一段是 {block.get('type') or '未知'} 类型的内容，本版不解析）")
        if not parts and (structured := payload.get("structuredContent")) is not None:
            parts.append(json.dumps(structured, ensure_ascii=False))
        text = "\n\n".join(part for part in parts if part)
        truncated = len(text) > TEXT_MAX_CHARS
        return McpCallOutcome(text=_stringify(text, TEXT_MAX_CHARS), is_error=bool(payload.get("isError")),
                              truncated=truncated)

    # ---- JSON-RPC ----

    def _initialize(self, target: "_Target", credential: str) -> tuple[str, dict]:
        """握手拿会话 id。每次调用都重握一次：这一版不缓存会话，省掉一整套失效与并发问题，
        代价是每次多一个往返。"""
        reply = self._post(target, credential, "", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        })
        return reply.session, self._result(reply)

    def _notify_initialized(self, target: "_Target", credential: str, session: str) -> None:
        """协议要求握手后发这条通知。server 回 202 无正文，失败不致命——有的实现不校验它。"""
        try:
            self._post(target, credential, session,
                       {"jsonrpc": "2.0", "method": "notifications/initialized"})
        except McpTransportError:
            logger.debug("MCP notifications/initialized 未被接受，继续")

    def _request(self, target: "_Target", credential: str, session: str, method: str, params: dict) -> dict:
        return self._result(self._post(target, credential, session,
                                       {"jsonrpc": "2.0", "id": 2, "method": method, "params": params}))

    def _post(self, target: "_Target", credential: str, session: str, body: dict) -> "_Reply":
        headers = {
            "Content-Type": "application/json", "Accept": _ACCEPT,
            "Host": target.host, "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if session:
            headers["Mcp-Session-Id"] = session
        if credential:
            headers["Authorization"] = credential if credential.lower().startswith("bearer ") else f"Bearer {credential}"
        last: Exception | None = None
        for address in target.addresses:
            literal = f"[{address}]" if ":" in address else address
            url = target.url.copy_with(host=literal, port=target.url.port)
            try:
                return self._exchange(url, body, headers, target.extensions)
            except httpx.HTTPError as error:
                last = error
        # 报类型名而不是 str(error)：httpx 的消息里带完整 URL，凭据在 query 上时会跟着漏。
        raise McpTransportError("unreachable", f"连不上这个 MCP server：{type(last).__name__}") from last

    def _exchange(self, url, body: dict, headers: dict, extensions: dict) -> "_Reply":
        """流式读取并在超限那一刻掐断。

        先 read 再判长度是拦不住的：等 httpx 把几十兆读进内存，该发生的已经发生了。
        状态码在读正文之前就判掉，出错的响应体一个字节都不必收。
        """
        stream = self._http().stream("POST", url, json=body, headers=headers, extensions=extensions)
        with stream as response:
            if response.is_redirect:
                raise McpTransportError("redirect_not_followed",
                                        "该地址要求重定向，未自动跟随；请在管理页填写最终地址")
            if response.status_code in {401, 403}:
                raise McpTransportError("unauthorized",
                                        f"server 拒绝了这次请求（HTTP {response.status_code}），请检查凭据")
            if response.status_code >= 400:
                raise McpTransportError("http_error", f"server 返回 HTTP {response.status_code}")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > RESPONSE_MAX_BYTES:
                    raise McpTransportError(
                        "response_too_large", f"server 返回超过 {RESPONSE_MAX_BYTES // 1024} KiB，已拒绝")
                chunks.append(chunk)
            return _Reply(
                session=response.headers.get("mcp-session-id", ""),
                content_type=response.headers.get("content-type", ""),
                encoding=response.encoding or "utf-8",
                raw=b"".join(chunks),
            )

    @staticmethod
    def _result(reply: "_Reply") -> dict:
        message = _decode(reply)
        if (error := message.get("error")) is not None:
            code = error.get("code") if isinstance(error, dict) else ""
            detail = error.get("message") if isinstance(error, dict) else ""
            raise McpTransportError("rpc_error", f"server 报错（{code}）：{_stringify(detail or '', 200)}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpTransportError("bad_response", "server 的响应里没有可用的 result")
        return result

    def _target(self, url: str) -> "_Target":
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"}:
            raise McpTransportError("unsupported_scheme", f"只支持 http/https，收到 {parsed.scheme or '空'}")
        host = parsed.host
        if not host:
            raise McpTransportError("invalid_url", "地址缺少主机名")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = resolved_public_ips(host, port, allow_loopback=self._allow_loopback)
        except BlockedAddress as error:
            raise McpTransportError(error.code, str(error)) from error
        return _Target(url=parsed, host=host, addresses=addresses,
                       extensions={"sni_hostname": host} if parsed.scheme == "https" else {})


class _Target:
    """校验完的目标：连 IP，域名只用于 Host 头与 TLS SNI（校验后再交给 httpx 解析一次
    就是 rebinding 的窗口）。"""

    __slots__ = ("url", "host", "addresses", "extensions")

    def __init__(self, *, url: httpx.URL, host: str, addresses: list[str], extensions: dict) -> None:
        self.url, self.host, self.addresses, self.extensions = url, host, addresses, extensions


@dataclass(frozen=True)
class _Reply:
    """一次交换里我们要留下的东西。响应对象本身不往外传：它是流式的，出了作用域就关了。"""

    session: str
    content_type: str
    encoding: str
    raw: bytes


def _decode(reply: _Reply) -> dict:
    """JSON 与 SSE 两种响应形态都要认：协议允许 server 任选一种回同一条 JSON-RPC 消息。"""
    body = reply.raw.decode(reply.encoding, errors="replace")
    if "text/event-stream" in reply.content_type:
        body = _first_sse_message(body)
    try:
        message = json.loads(body) if body.strip() else {}
    except json.JSONDecodeError:
        raise McpTransportError("bad_response", "server 的响应不是合法 JSON") from None
    if isinstance(message, list):
        # 批量响应：只认第一条带 result/error 的。
        message = next((item for item in message if isinstance(item, dict)), {})
    return message if isinstance(message, dict) else {}


def _first_sse_message(body: str) -> str:
    """SSE 里第一条带 data 的事件就是那条 JSON-RPC 响应。"""
    for frame in body.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
        if data.strip():
            return data
    return ""
