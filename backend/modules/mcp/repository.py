from __future__ import annotations

import json

from core.common import utc_now
from core.store import SQLiteStore

from .models import McpServer, SnapshotTool


class McpRepository:
    """MCP server 与它们的工具快照。

    凭据单开一个读方法：别的读路径一律不带它出来，避免哪天顺手把整行序列化进响应。
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def list_all(self) -> list[McpServer]:
        with self._store.read() as connection:
            rows = connection.execute("SELECT * FROM mcp_servers ORDER BY slug").fetchall()
        return [_from_row(row) for row in rows]

    def get(self, server_id: str) -> McpServer | None:
        with self._store.read() as connection:
            row = connection.execute("SELECT * FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        return _from_row(row) if row is not None else None

    def find_by_url(self, url: str) -> McpServer | None:
        with self._store.read() as connection:
            row = connection.execute("SELECT * FROM mcp_servers WHERE url = ?", (url,)).fetchone()
        return _from_row(row) if row is not None else None

    def taken_slugs(self) -> set[str]:
        with self._store.read() as connection:
            return {row[0] for row in connection.execute("SELECT slug FROM mcp_servers")}

    def credential(self, server_id: str) -> str:
        """只有调用路径读它。返回值不得进日志、trace、SSE 或任何 HTTP 响应。"""
        with self._store.read() as connection:
            row = connection.execute("SELECT credential FROM mcp_servers WHERE id = ?", (server_id,)).fetchone()
        return str(row[0]) if row is not None else ""

    def insert(self, server: McpServer, *, credential: str = "") -> McpServer:
        now = utc_now()
        with self._store.write() as connection:
            connection.execute(
                "INSERT INTO mcp_servers(id, slug, label, url, status, origin, credential, note,"
                " tools_json, tools_total, protocol_version, server_info, last_error, connected_at,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (server.id, server.slug, server.label, server.url, server.status, server.origin,
                 credential, server.note, _dump(server.tools), server.tools_total,
                 server.protocol_version, server.server_info, server.last_error, server.connected_at,
                 now, now),
            )
        return self.get(server.id) or server

    def save_snapshot(self, *, server_id: str, tools: tuple[SnapshotTool, ...], tools_total: int,
                      protocol_version: str, server_info: str) -> None:
        """连接成功：整份快照原地替换，状态转 connected 并清掉上次的错误。"""
        now = utc_now()
        with self._store.write() as connection:
            connection.execute(
                "UPDATE mcp_servers SET tools_json = ?, tools_total = ?, protocol_version = ?,"
                " server_info = ?, status = 'connected', last_error = '', connected_at = ?, updated_at = ?"
                " WHERE id = ?",
                (_dump(tools), tools_total, protocol_version, server_info, now, now, server_id),
            )

    def set_status(self, *, server_id: str, status: str, last_error: str = "") -> None:
        with self._store.write() as connection:
            connection.execute(
                "UPDATE mcp_servers SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                (status, last_error, utc_now(), server_id),
            )

    def set_credential(self, *, server_id: str, credential: str) -> None:
        with self._store.write() as connection:
            connection.execute("UPDATE mcp_servers SET credential = ?, updated_at = ? WHERE id = ?",
                               (credential, utc_now(), server_id))

    def delete(self, server_id: str) -> bool:
        with self._store.write() as connection:
            return connection.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,)).rowcount > 0


def _dump(tools: tuple[SnapshotTool, ...]) -> str:
    return json.dumps([{"name": item.name, "safe_name": item.safe_name,
                        "description": item.description, "input_schema": item.input_schema}
                       for item in tools], ensure_ascii=False)


def _from_row(row) -> McpServer:
    try:
        raw = json.loads(row["tools_json"])
    except (json.JSONDecodeError, TypeError):
        raw = []
    tools = tuple(
        SnapshotTool(name=str(item.get("name") or ""), safe_name=str(item.get("safe_name") or ""),
                     description=str(item.get("description") or ""),
                     input_schema=item.get("input_schema") if isinstance(item.get("input_schema"), dict) else {})
        for item in raw if isinstance(item, dict) and item.get("safe_name")
    )
    return McpServer(
        id=row["id"], slug=row["slug"], label=row["label"], url=row["url"], status=row["status"],
        origin=row["origin"], note=row["note"], tools=tools, tools_total=int(row["tools_total"] or 0),
        protocol_version=row["protocol_version"], server_info=row["server_info"],
        last_error=row["last_error"], has_credential=bool(row["credential"]),
        connected_at=row["connected_at"], created_at=row["created_at"], updated_at=row["updated_at"],
    )
