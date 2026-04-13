"""
【模块说明】
- 主要作用：验证本地 stdio MCP 链路可用性与错误语义。
- 覆盖范围：initialize、tools/list、tools/call、无 fallback 约束。
- 说明：使用 unittest，确保 `python -m unittest tests.test_mcp_stdio` 能直接执行。
"""

import json
import unittest


class MCPStdioTests(unittest.TestCase):
    def test_server_module_importable(self):
        import mcp_tools.server_stdio as server_stdio  # noqa: F401

    def test_stdio_protocol_initialize_list_and_call(self):
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
            self.assertIn("result", init_resp)
            self.assertEqual("2024-11-05", init_resp["result"]["protocolVersion"])
            self.assertIn("tools", init_resp["result"]["capabilities"])

            client.notify("notifications/initialized", {})

            list_resp = client.rpc("tools/list", {})
            self.assertIn("result", list_resp)
            tools = list_resp["result"]["tools"]
            self.assertEqual(6, len(tools))
            names = {tool["name"] for tool in tools}
            self.assertEqual(
                {
                    "calculator",
                    "websearch",
                    "memory_search",
                    "mindmap_generator",
                    "get_datetime",
                    "filewriter",
                },
                names,
            )
            for tool in tools:
                self.assertIn("name", tool)
                self.assertIn("inputSchema", tool)

            call_resp = client.rpc(
                "tools/call",
                {"name": "calculator", "arguments": {"expression": "2+2"}},
            )
            payload = call_resp["result"]["content"][0]["text"]
            tool_result = json.loads(payload)
            self.assertTrue(tool_result["success"])
            self.assertEqual(4, tool_result["result"])
        finally:
            client.close()

    def test_stdio_protocol_errors(self):
        from mcp_tools.client import _StdioMCPClient

        client = _StdioMCPClient(request_timeout=10.0)
        try:
            resp = client.rpc("method/not-found", {})
            self.assertEqual(-32601, resp["error"]["code"])

            resp = client.rpc("tools/call", {"name": "", "arguments": {}})
            self.assertEqual(-32602, resp["error"]["code"])

            resp = client.rpc("tools/call", {"name": "calculator", "arguments": "bad"})
            self.assertEqual(-32602, resp["error"]["code"])
        finally:
            client.close()

    def test_mcp_tools_call_tool_uses_stdio_path(self):
        from mcp_tools.client import MCPTools, _shutdown_mcp_client

        _shutdown_mcp_client()
        try:
            result = MCPTools.call_tool("calculator", expression="3*3")
            self.assertTrue(result["success"])
            self.assertEqual(9, result["result"])
            self.assertEqual("mcp_stdio", result["via"])
        finally:
            _shutdown_mcp_client()

    def test_mcp_tools_call_tool_no_local_fallback_when_server_unavailable(self):
        import mcp_tools.client as client_mod

        client_mod._shutdown_mcp_client()
        bad_client = client_mod._StdioMCPClient(
            server_module="mcp_tools.__missing_server__",
            request_timeout=1.0,
        )
        client_mod._MCP_CLIENT = bad_client
        try:
            result = client_mod.MCPTools.call_tool("calculator", expression="1+1")
            self.assertFalse(result["success"])
            self.assertEqual("mcp_stdio", result["via"])
            self.assertIn("MCP", result["error"])
        finally:
            try:
                bad_client.close()
            except Exception:
                pass
            client_mod._MCP_CLIENT = None


if __name__ == "__main__":
    unittest.main()
