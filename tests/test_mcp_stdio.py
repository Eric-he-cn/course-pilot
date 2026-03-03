"""
【模块说明】
- 主要作用：验证本地 stdio MCP 链路可用性与错误语义。
- 覆盖范围：initialize、tools/list、tools/call、无 fallback 约束。
"""
import json


def test_server_module_importable():
    import mcp_tools.server_stdio as server_stdio  # noqa: F401


def test_stdio_protocol_initialize_list_and_call():
    from mcp_tools.client import _StdioMCPClient

    client = _StdioMCPClient(request_timeout=10.0)
    try:
        init_resp = client.rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "pytest", "version": "0.1.0"},
            },
        )
        assert "result" in init_resp
        assert init_resp["result"]["protocolVersion"] == "2024-11-05"
        assert "tools" in init_resp["result"]["capabilities"]

        client.notify("notifications/initialized", {})

        list_resp = client.rpc("tools/list", {})
        assert "result" in list_resp
        tools = list_resp["result"]["tools"]
        assert len(tools) == 6
        names = {tool["name"] for tool in tools}
        assert names == {
            "calculator",
            "websearch",
            "memory_search",
            "mindmap_generator",
            "get_datetime",
            "filewriter",
        }
        for tool in tools:
            assert "name" in tool
            assert "inputSchema" in tool

        call_resp = client.rpc(
            "tools/call",
            {"name": "calculator", "arguments": {"expression": "2+2"}},
        )
        payload = call_resp["result"]["content"][0]["text"]
        tool_result = json.loads(payload)
        assert tool_result["success"] is True
        assert tool_result["result"] == 4
    finally:
        client.close()


def test_stdio_protocol_errors():
    from mcp_tools.client import _StdioMCPClient

    client = _StdioMCPClient(request_timeout=10.0)
    try:
        resp = client.rpc("method/not-found", {})
        assert resp["error"]["code"] == -32601

        resp = client.rpc("tools/call", {"name": "", "arguments": {}})
        assert resp["error"]["code"] == -32602

        resp = client.rpc("tools/call", {"name": "calculator", "arguments": "bad"})
        assert resp["error"]["code"] == -32602
    finally:
        client.close()


def test_mcp_tools_call_tool_uses_stdio_path():
    from mcp_tools.client import MCPTools, _shutdown_mcp_client

    _shutdown_mcp_client()
    try:
        result = MCPTools.call_tool("calculator", expression="3*3")
        assert result["success"] is True
        assert result["result"] == 9
        assert result["via"] == "mcp_stdio"
    finally:
        _shutdown_mcp_client()


def test_mcp_tools_call_tool_no_local_fallback_when_server_unavailable():
    import mcp_tools.client as client_mod

    client_mod._shutdown_mcp_client()
    bad_client = client_mod._StdioMCPClient(
        server_module="mcp_tools.__missing_server__",
        request_timeout=1.0,
    )
    client_mod._MCP_CLIENT = bad_client
    try:
        result = client_mod.MCPTools.call_tool("calculator", expression="1+1")
        assert result["success"] is False
        assert result["via"] == "mcp_stdio"
        assert "MCP" in result["error"]
    finally:
        try:
            bad_client.close()
        except Exception:
            pass
        client_mod._MCP_CLIENT = None
