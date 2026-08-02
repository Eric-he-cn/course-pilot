"""MCP 接入：用户连一台 server，Agent 就能用上它声明的工具。

三条骨架：
1. 注册期在「用户点连接那一刻」——拉一次 tools/list 落库成快照。运行期只认快照，
   不再向 server 发现工具，server 事后偷换定义也换不掉已经批准过的那份。
2. 模型只能提议（mcp_propose 落一行 proposed），批准在管理页，由人来点。
   proposed 的行不下发任何工具，也从没发过一次网络请求。
3. server 返回的 annotations 一律不看：协议自己就说它是不可信提示。权限由能力档决定。
"""
from __future__ import annotations

import logging
import re
import uuid

import httpx

from contracts.mcp import McpTransportError, McpTransportPort

from .api import NAMESPACE_PREFIX, ExternalCallResult, ExternalTool, ProposalOutcome
from .models import McpServer, SnapshotTool
from .repository import McpRepository

logger = logging.getLogger(__name__)

# 一台 server 最多收几个工具。声明上百个的 server 会把上下文撑爆，也会把内置工具淹掉；
# 超出的条数如实报给用户，让他自己判断这台是不是该接。
MAX_TOOLS_PER_SERVER = 30
# 一共能接几台。再多就不是「接一个工具」而是在这里搭一套插件市场了。
MAX_SERVERS = 8
# 工具名的总长上限：OpenAI 兼容接口对 function.name 的约束是 64 位 [A-Za-z0-9_-]。
TOOL_NAME_MAX = 64
SLUG_MAX = 20
LABEL_MAX = 40
URL_MAX = 500
CREDENTIAL_MAX = 4_000
NOTE_MAX = 300
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")
_SLUG_SEPARATOR = re.compile(r"[^a-z0-9]+")
# 只作语法预检，DNS 那道真校验在传输层（连接时才做）。字面写着回环或内网就直接回绝，
# 免得用户白批准一次；模型提议的地址更要在落库前先被这一条挡掉。
_LITERAL_LOCAL = re.compile(
    r"^(localhost|127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|::1$|\[::1\]|172\.(1[6-9]|2\d|3[01])\.)",
    re.IGNORECASE)


class McpConfigError(ValueError):
    """配置不合法（地址、名称、数量上限）。code 供 HTTP 层翻成错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpService:
    def __init__(self, repository: McpRepository, transport: McpTransportPort, *,
                 allow_loopback: bool = False) -> None:
        self._repository = repository
        self._transport = transport
        # 传输层自己也在校验，这里同一个开关只用于语法预检要不要放行回环。
        self._allow_loopback = allow_loopback

    # ---- 管理动作 ----

    def list_servers(self) -> list[McpServer]:
        return self._repository.list_all()

    def connect(self, *, label: str, url: str, credential: str = "", note: str = "") -> McpServer:
        """形态一：用户在管理页填地址点连接。落库与握手在同一个动作里完成。"""
        server = self._create(label=label, url=url, credential=credential, note=note,
                              origin="user", status="proposed")
        return self.refresh(server_id=server.id)

    def propose(self, *, label: str, url: str, note: str = "") -> ProposalOutcome:
        """形态二：模型从聊天里解析出一段配置。只落一行待批准的记录，不连、不下发工具。"""
        try:
            cleaned_url = self._check_url(url)
            if (existing := self._repository.find_by_url(cleaned_url)) is not None:
                return ProposalOutcome(
                    accepted=False, label=existing.label, url=cleaned_url,
                    message=f"这个地址已经在列表里了（名称「{existing.label}」，当前状态 {existing.status}）。"
                            "要改动请让用户去管理页操作。")
            server = self._create(label=label, url=cleaned_url, note=note, origin="model", status="proposed")
        except McpConfigError as error:
            return ProposalOutcome(accepted=False, message=str(error))
        return ProposalOutcome(
            accepted=True, label=server.label, url=server.url,
            message="已把这台 server 记为待批准。它现在连都没连过，工具也一个都没下发——"
                    "请用户到设置页的「MCP server」里核对地址后点批准，批准之后你下一轮才会看到它的工具。")

    def refresh(self, *, server_id: str, credential: str | None = None) -> McpServer:
        """批准或重连：这一刻拉一次 tools/list，落库成快照。运行期不再发现工具。"""
        server = self._require(server_id)
        if credential is not None:
            self._repository.set_credential(server_id=server_id, credential=credential[:CREDENTIAL_MAX])
        secret = self._repository.credential(server_id)
        try:
            handshake = self._transport.handshake(url=server.url, credential=secret)
        except McpTransportError as error:
            self._repository.set_status(server_id=server_id, status="error",
                                        last_error=_redact(f"{error.code}：{error}", secret))
            return self._require(server_id)
        tools, total = self._snapshot(handshake.tools, slug=server.slug)
        self._repository.save_snapshot(
            server_id=server_id, tools=tools, tools_total=total,
            protocol_version=handshake.protocol_version,
            server_info=f"{handshake.server_name} {handshake.server_version}".strip())
        return self._require(server_id)

    def set_enabled(self, *, server_id: str, enabled: bool) -> McpServer:
        server = self._require(server_id)
        if enabled and not server.tools:
            # 没有快照就没什么可启用的：必须先连一次。
            return self.refresh(server_id=server_id)
        self._repository.set_status(server_id=server_id,
                                    status="connected" if enabled else "disabled")
        return self._require(server_id)

    def remove(self, *, server_id: str) -> None:
        if not self._repository.delete(server_id):
            raise LookupError(server_id)

    # ---- 运行期 ----

    def external_tools(self) -> tuple[ExternalTool, ...]:
        """只有 connected 的 server 下发工具。proposed（没人批准）、disabled、error 都不下发。"""
        items = []
        for server in self._repository.list_all():
            if server.status != "connected":
                continue
            for tool in server.tools:
                items.append(ExternalTool(
                    name=f"{NAMESPACE_PREFIX}{server.slug}__{tool.safe_name}",
                    description=tool.description, input_schema=tool.input_schema,
                    server_slug=server.slug, server_label=server.label))
        return tuple(items)

    def call_tool(self, *, name: str, arguments: dict) -> ExternalCallResult:
        located = self._locate(name)
        if located is None:
            return ExternalCallResult(text=f"没有名为 {name} 的外部工具，或它所属的 server 已不可用。",
                                      ok=False, code="unknown_external_tool")
        server, tool = located
        secret = self._repository.credential(server.id)
        try:
            outcome = self._transport.call(url=server.url, credential=secret,
                                           tool=tool.name, arguments=arguments)
        except McpTransportError as error:
            # 失败原因要能让模型改路，但一个字凭据都不能带出去。
            message = _redact(str(error), secret)
            logger.warning("MCP 调用失败 server=%s tool=%s code=%s", server.slug, tool.safe_name, error.code)
            return ExternalCallResult(text=f"外部工具调用失败（{error.code}）：{message}", ok=False,
                                      server_label=server.label, code=error.code)
        text = _redact(outcome.text, secret)
        if outcome.truncated:
            text += "\n\n（这次返回超过单次上限，末尾已截断；需要完整内容请缩小参数范围再调一次。）"
        if outcome.is_error:
            return ExternalCallResult(text=text or "外部工具报告这次调用失败，但没有给出原因。",
                                      ok=False, server_label=server.label, code="tool_error")
        return ExternalCallResult(text=text or "（这个工具这次没有返回任何内容。）", ok=True,
                                  server_label=server.label, truncated=outcome.truncated)

    # ---- 内部 ----

    def _locate(self, name: str) -> tuple[McpServer, SnapshotTool] | None:
        """按快照解析命名空间。slug 只含 [a-z0-9-]，所以切三段无歧义。"""
        parts = name.split("__", 2)
        if len(parts) != 3 or parts[0] != NAMESPACE_PREFIX.rstrip("_"):
            return None
        slug, safe_name = parts[1], parts[2]
        server = next((item for item in self._repository.list_all()
                       if item.slug == slug and item.status == "connected"), None)
        if server is None:
            return None
        tool = next((item for item in server.tools if item.safe_name == safe_name), None)
        return (server, tool) if tool is not None else None

    def _create(self, *, label: str, url: str, credential: str = "", note: str = "",
                origin: str, status: str) -> McpServer:
        existing = self._repository.taken_slugs()
        if len(existing) >= MAX_SERVERS:
            raise McpConfigError("too_many_servers", f"最多只能接 {MAX_SERVERS} 台 MCP server，先删掉一台再加。")
        cleaned_url = self._check_url(url)
        if self._repository.find_by_url(cleaned_url) is not None:
            raise McpConfigError("duplicate_url", "这个地址已经在列表里了。")
        clean_label = " ".join(str(label).split())[:LABEL_MAX] or "MCP server"
        server = McpServer(
            id=f"mcp_{uuid.uuid4().hex[:12]}", slug=_unique_slug(clean_label, cleaned_url, existing),
            label=clean_label, url=cleaned_url, status=status, origin=origin,
            note=" ".join(str(note).split())[:NOTE_MAX],
        )
        return self._repository.insert(server, credential=str(credential or "")[:CREDENTIAL_MAX])

    def _check_url(self, url: str) -> str:
        text = str(url or "").strip()
        if not text or len(text) > URL_MAX:
            raise McpConfigError("invalid_url", f"地址不能为空，且不超过 {URL_MAX} 个字符。")
        try:
            parsed = httpx.URL(text)
        except (httpx.InvalidURL, ValueError):
            raise McpConfigError("invalid_url", "这不是一个合法的地址。") from None
        if parsed.scheme not in {"http", "https"}:
            raise McpConfigError("unsupported_scheme", "只支持 http/https 的 MCP server 地址。")
        if not parsed.host:
            raise McpConfigError("invalid_url", "地址缺少主机名。")
        if _LITERAL_LOCAL.match(parsed.host) and not self._allow_loopback:
            raise McpConfigError("blocked_address", f"「{parsed.host}」指向本机或内网，已拒绝。")
        return text

    def _require(self, server_id: str) -> McpServer:
        server = self._repository.get(server_id)
        if server is None:
            raise LookupError(server_id)
        return server

    @staticmethod
    def _snapshot(descriptors, *, slug: str) -> tuple[tuple[SnapshotTool, ...], int]:
        """把 server 声明的工具规范化成快照。名字冲突或规范化后为空的直接丢，
        超过上限的按声明顺序截断——总数照实存下来，界面上要能看出丢了多少。"""
        room = TOOL_NAME_MAX - len(NAMESPACE_PREFIX) - len(slug) - 2
        tools: list[SnapshotTool] = []
        seen: set[str] = set()
        for item in descriptors:
            if len(tools) >= MAX_TOOLS_PER_SERVER:
                break
            safe = _SAFE_NAME.sub("_", item.name).strip("_")[:room]
            if not safe or safe in seen:
                continue
            seen.add(safe)
            schema = item.input_schema if isinstance(item.input_schema, dict) else {}
            if schema.get("type") != "object":
                # 供应商一律要求顶层是 object；不是就换成空对象，工具还能调，只是不带参数。
                schema = {"type": "object", "properties": {}}
            tools.append(SnapshotTool(name=item.name, safe_name=safe,
                                      description=item.description, input_schema=schema))
        return tuple(tools), len(list(descriptors))


def _slugify(text: str) -> str:
    return _SLUG_SEPARATOR.sub("-", text.lower()).strip("-")[:SLUG_MAX].strip("-")


def _unique_slug(label: str, url: str, taken: set[str]) -> str:
    # 中文名称规范化后会空掉，退到地址的主机名——它进命名空间，可读比整齐要紧。
    base = _slugify(label) or _slugify(httpx.URL(url).host or "") or "server"
    if base not in taken:
        return base
    for index in range(2, MAX_SERVERS + 2):
        candidate = f"{base[:SLUG_MAX - 2]}-{index}"
        if candidate not in taken:
            return candidate
    return f"s-{uuid.uuid4().hex[:8]}"


def _redact(text: str, secret: str) -> str:
    """凭据不许出现在回执、日志与落库的错误里。server 可以把 token 原样回显在报错里，
    所以出站方向也要滤一次，不能只管入站。"""
    if not secret or len(secret) < 6:
        return text
    cleaned = text.replace(secret, "***")
    bare = secret.split(" ", 1)[-1]
    return cleaned.replace(bare, "***") if len(bare) >= 6 else cleaned
