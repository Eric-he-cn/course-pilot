"""一台真的假 MCP server：本机起一个 HTTP 进程，按协议回 JSON-RPC。

用真 server 而不是替一个 transport：SSRF 校验、字节上限、超时、SSE 解析都在传输层，
换掉 transport 就等于把要测的那一层整个跳过。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 协议里 initialize 之后的通知，没有 id，不需要回 result。
_NOTIFICATION = object()


class FakeMcpServer:
    """可以在运行中被改掉工具定义——rug pull 那条测试就靠它。"""

    def __init__(self, *, tools=None, results=None, mode: str = "json",
                 delay: float = 0.0, padding: int = 0, require_token: str = "") -> None:
        self.tools = list(tools if tools is not None else [_tool("echo", "把参数原样回显")])
        self.results = dict(results or {})
        self.mode = mode                    # json | sse
        self.delay = delay                  # 每次响应前先睡多久，用来触发超时
        self.padding = padding              # 往返回正文里塞多少字符，用来触发上限
        self.require_token = require_token
        self.requests: list[dict] = []      # 收到过的每一条 JSON-RPC，测试据此断言「一次都没连过」
        self.auth_headers: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ---- 生命周期 ----

    def __enter__(self) -> "FakeMcpServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # 测试输出里不要 access log
                return

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的命名
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    message = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    message = {}
                owner.requests.append(message)
                owner.auth_headers.append(self.headers.get("authorization") or "")
                if owner.delay:
                    time.sleep(owner.delay)
                if owner.require_token and self.headers.get("authorization") != f"Bearer {owner.require_token}":
                    self._send(401, b'{"jsonrpc":"2.0","id":1,"error":{"code":-32001,"message":"bad token"}}')
                    return
                reply = owner.reply_to(message)
                if reply is _NOTIFICATION:
                    self._send(202, b"")
                    return
                body = json.dumps(reply, ensure_ascii=False).encode()
                if owner.mode == "sse":
                    self._send(200, b"event: message\ndata: " + body + b"\n\n", content_type="text/event-stream")
                else:
                    self._send(200, body)

            def _send(self, status: int, body: bytes, *, content_type: str = "application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Mcp-Session-Id", "session-1")
                # 一请求一连接。留着 keep-alive 的话，处理线程会在 server_close 之后
                # 继续服务那条已建立的连接，「server 关掉之后连不上」这类用例就测不成。
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()
                if body:
                    self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    @property
    def url(self) -> str:
        assert self._server is not None, "server 还没起来"
        return f"http://127.0.0.1:{self._server.server_address[1]}/mcp"

    # ---- 协议 ----

    def reply_to(self, message: dict):
        method = message.get("method")
        if method == "notifications/initialized":
            return _NOTIFICATION
        if method == "initialize":
            return self._ok(message, {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            })
        if method == "tools/list":
            return self._ok(message, {"tools": self.tools})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            body = self.results.get(name, f"{name} 的返回：" + json.dumps(params.get("arguments") or {}, ensure_ascii=False))
            if self.padding:
                body += "填" * self.padding
            return self._ok(message, {"content": [{"type": "text", "text": body}], "isError": False})
        return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": "method not found"}}

    @staticmethod
    def _ok(message: dict, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}

    def calls_of(self, method: str) -> list[dict]:
        return [item for item in self.requests if item.get("method") == method]


def _tool(name: str, description: str, properties: dict | None = None) -> dict:
    return {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties or {"text": {"type": "string"}}},
        # server 自报的这几条一律不可信，也不该被我们采信——协议自己就这么要求。
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "title": "看起来很安全"},
    }


tool = _tool
