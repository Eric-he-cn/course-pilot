"""
【模块说明】
- 主要作用：提供本地 stdio 版 MCP Server，承载工具调用协议入口。
- 协议子集：initialize / notifications/initialized / tools/list / tools/call。
- 核心函数：_read_message、_write_message、_handle_request、main。
"""
import json
import sys
import builtins
from typing import Any, Dict, Optional

from mcp_tools.client import MCPTools, _to_mcp_tools

# stdio MCP 协议要求 stdout 仅用于协议帧，避免业务 print 污染通道。
_orig_print = builtins.print


def _stderr_print(*args, **kwargs):
    """将所有业务打印重定向到 stderr，避免污染 stdout 协议通道。"""
    kwargs.setdefault("file", sys.stderr)
    return _orig_print(*args, **kwargs)


builtins.print = _stderr_print


def _write_message(msg: Dict[str, Any]) -> None:
    """按 Content-Length 帧格式写出一条 JSON-RPC 消息。"""
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def _read_message() -> Optional[Dict[str, Any]]:
    """读取并解析一条 Content-Length 帧消息。"""
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("ascii", errors="replace").strip()
        if ":" in decoded:
            key, value = decoded.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        return None
    return json.loads(payload.decode("utf-8"))


def _ok(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    """构造 JSON-RPC 错误响应。"""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _handle_request(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """处理单条请求并返回响应（通知类请求返回 None）。"""
    method = msg.get("method", "")
    req_id = msg.get("id")
    params_raw = msg.get("params")
    if params_raw is None:
        params = {}
    elif isinstance(params_raw, dict):
        params = params_raw
    else:
        if req_id is None:
            return None
        return _err(req_id, -32602, "Invalid params: params must be object")

    if method == "notifications/initialized":
        return None

    # 通知消息无需响应
    if req_id is None:
        return None

    if method == "initialize":
        return _ok(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "coursepilot-local-mcp", "version": "0.1.0"},
            },
        )

    if method == "tools/list":
        return _ok(req_id, {"tools": _to_mcp_tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _err(req_id, -32602, "Invalid params: tool name is required")
        if not isinstance(arguments, dict):
            return _err(req_id, -32602, "Invalid params: arguments must be object")

        result = MCPTools._call_tool_local(name, **arguments)
        return _ok(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False),
                    }
                ],
                "isError": not bool(result.get("success", False)),
            },
        )

    return _err(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    """stdio MCP 主循环。"""
    while True:
        msg = _read_message()
        if msg is None:
            break
        try:
            resp = _handle_request(msg)
        except Exception as ex:
            req_id = msg.get("id")
            resp = _err(req_id, -32000, f"Server error: {ex}")
        if resp is not None:
            _write_message(resp)


if __name__ == "__main__":
    main()
