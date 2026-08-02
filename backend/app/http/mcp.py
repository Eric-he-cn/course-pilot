from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.bootstrap import Application
from app.http.deps import current_workspace
from modules.agent.tools import EXTERNAL_TOOL_MAX, external_specs
from modules.mcp.api import NAMESPACE_PREFIX
from modules.mcp.models import McpServer
from modules.mcp.service import MAX_TOOLS_PER_SERVER, McpConfigError


def build_mcp_router() -> APIRouter:
    """MCP server 的管理面：连接、批准模型的提议、启停、删除。

    凭据只进不出：写它走 PUT，读的路径一律只报 has_credential。
    """
    router = APIRouter(prefix="/api/v2", tags=["mcp"])

    def fail(status: int, code: str, message: str) -> HTTPException:
        return HTTPException(status_code=status, detail={"error": {"code": code, "message": message, "retryable": False}})

    def require(application: Application, server_id: str) -> McpServer:
        server = next((item for item in application.mcp.list_servers() if item.id == server_id), None)
        if server is None:
            raise fail(404, "not_found", "没有这台 MCP server")
        return server

    def payload(application: Application) -> dict[str, object]:
        """一次把整张表给出去：哪几个工具真能下发是跨 server 一起算的，分开取会各说各话。"""
        servers = application.mcp.list_servers()
        downlinked = {spec.name for spec in external_specs(application.mcp.external_tools())[0]}
        return {
            "servers": [_render(server, downlinked) for server in servers],
            "limits": {"max_tools_per_server": MAX_TOOLS_PER_SERVER, "max_downlinked_tools": EXTERNAL_TOOL_MAX},
        }

    @router.get("/mcp/servers")
    def list_servers(application: Application = Depends(current_workspace)) -> dict[str, object]:
        return payload(application)

    @router.post("/mcp/servers", status_code=201)
    def connect(body: dict, application: Application = Depends(current_workspace)) -> dict[str, object]:
        try:
            application.mcp.connect(
                label=str(body.get("label") or ""), url=str(body.get("url") or ""),
                credential=str(body.get("credential") or ""), note=str(body.get("note") or ""),
            )
        except McpConfigError as error:
            raise fail(422, error.code, str(error)) from None
        return payload(application)

    @router.post("/mcp/servers/{server_id}/connect")
    def reconnect(server_id: str, body: dict | None = None,
                  application: Application = Depends(current_workspace)) -> dict[str, object]:
        """批准一条提议，或给已有的 server 重新拉一次快照。这是唯一会真去连 server 的入口。"""
        require(application, server_id)
        credential = body.get("credential") if isinstance(body, dict) else None
        application.mcp.refresh(server_id=server_id,
                                credential=str(credential) if credential is not None else None)
        return payload(application)

    @router.patch("/mcp/servers/{server_id}")
    def set_enabled(server_id: str, body: dict, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require(application, server_id)
        application.mcp.set_enabled(server_id=server_id, enabled=bool(body.get("enabled")))
        return payload(application)

    @router.delete("/mcp/servers/{server_id}", status_code=204)
    def remove(server_id: str, application: Application = Depends(current_workspace)) -> None:
        try:
            application.mcp.remove(server_id=server_id)
        except LookupError:
            raise fail(404, "not_found", "没有这台 MCP server") from None

    return router


def _render(server: McpServer, downlinked: set[str]) -> dict[str, object]:
    """对外的形状。credential 一个字都不出去，只报有没有配。"""
    tools = [
        {"name": tool.safe_name, "raw_name": tool.name, "description": tool.description,
         "downlinked": f"{NAMESPACE_PREFIX}{server.slug}__{tool.safe_name}" in downlinked}
        for tool in server.tools
    ]
    # 失败原因拆成「码 + 详情」：码由界面按语言渲染，详情是后端原文（照
    # settings.embed_failed 的先例只作补充），否则英文界面会读到一整句中文。
    code, _, detail = server.last_error.partition("：")
    return {
        "id": server.id, "slug": server.slug, "label": server.label, "url": server.url,
        "status": server.status, "origin": server.origin, "note": server.note,
        "has_credential": server.has_credential, "protocol_version": server.protocol_version,
        "server_info": server.server_info,
        "last_error_code": code, "last_error_detail": detail or server.last_error,
        "connected_at": server.connected_at, "updated_at": server.updated_at,
        "tools": tools, "tools_total": server.tools_total,
        # 两种截断都要说出来：快照那一层丢的（server 声明得太多），
        # 与下发那一层丢的（schema 配额吃不下）。
        "dropped_at_snapshot": server.dropped_tools,
        "dropped_at_downlink": sum(1 for item in tools if not item["downlinked"]) if server.status == "connected" else 0,
    }
