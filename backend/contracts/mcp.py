"""MCP 接入的传输层契约。

传输做成可插拔的抽象，但目前只有 Streamable HTTP 一种实现：stdio 要起子进程，
撞上项目「没有 shell 执行」这条底线。抽象留着，将来加别的传输不必改上层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class McpToolDescriptor:
    """server 声明的一个工具。这是快照的最小单位——连接那一刻取一次就固定下来。

    annotations 一律不解析也不存：协议自己就说它是不可信提示，server 说自己「只读」
    不能当真。权限由我们这边的能力档决定。
    """

    name: str
    description: str
    input_schema: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class McpHandshake:
    """连接一次的结果：server 自报的身份与它当时声明的全部工具。"""

    protocol_version: str
    server_name: str
    server_version: str
    tools: tuple[McpToolDescriptor, ...] = ()


@dataclass(frozen=True)
class McpCallOutcome:
    """一次 tools/call 的返回。text 已经由传输层拼好、按字节上限截断过。"""

    text: str
    is_error: bool = False
    truncated: bool = False


class McpTransportError(RuntimeError):
    """连接或调用失败；code 用于回执、界面与 trace。消息里不得含凭据。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpTransportPort(Protocol):
    """一种和 MCP server 说话的方式。两个动作分开：连接那一刻拉一次工具清单，
    运行期只按名字调用——运行期不再发现工具，rug pull 就换不掉已经批准过的定义。"""

    def handshake(self, *, url: str, credential: str = "") -> McpHandshake:
        """握手并取回工具清单。失败抛 McpTransportError。"""
        ...

    def call(self, *, url: str, credential: str, tool: str, arguments: dict) -> McpCallOutcome:
        ...
