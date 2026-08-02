from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SnapshotTool:
    """快照里的一条工具定义。存的是 server 在「点连接那一刻」说的话，之后不再更新。"""

    name: str            # server 原始名
    safe_name: str       # 规范化后的名字，用于拼命名空间
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class McpServer:
    id: str
    slug: str
    label: str
    url: str
    status: str          # proposed | connected | disabled | error
    origin: str          # user | model
    note: str = ""
    tools: tuple[SnapshotTool, ...] = ()
    tools_total: int = 0  # server 当时声明的总数，可能大于快照里存下的条数
    protocol_version: str = ""
    server_info: str = ""
    last_error: str = ""
    has_credential: bool = False
    connected_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def dropped_tools(self) -> int:
        return max(0, self.tools_total - len(self.tools))
