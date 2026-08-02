"""MCP 模块对外的 Port 与数据形状。别的模块只经这里看见 MCP。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# 命名空间：`mcp__<slug>__<tool>`。slug 只含 [a-z0-9-]，所以按 `__` 切三段永远无歧义，
# 外部工具也撞不上内置工具或另一台 server 的同名工具。
NAMESPACE_PREFIX = "mcp__"


def is_external_tool(name: str) -> bool:
    return name.startswith(NAMESPACE_PREFIX)


@dataclass(frozen=True)
class ExternalTool:
    """一个已批准、可下发给模型的外部工具。name 已带命名空间。"""

    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)
    server_slug: str = ""
    server_label: str = ""


@dataclass(frozen=True)
class ExternalCallResult:
    """一次外部工具调用的结果。正文由调用方加上「只作资料」的声明后再给模型。"""

    text: str
    ok: bool
    server_label: str = ""
    truncated: bool = False
    code: str = ""


@dataclass(frozen=True)
class ProposalOutcome:
    """模型提议接一台 server 的结果。提议只落一行待批准的记录，不发任何网络请求。"""

    accepted: bool
    label: str = ""
    url: str = ""
    message: str = ""


class McpToolProviderPort(Protocol):
    def external_tools(self) -> tuple[ExternalTool, ...]:
        """已连接 server 的工具快照。运行期只读这一份，不向 server 发现工具。"""
        ...

    def call_tool(self, *, name: str, arguments: dict) -> ExternalCallResult: ...

    def propose(self, *, label: str, url: str, note: str = "") -> ProposalOutcome: ...
