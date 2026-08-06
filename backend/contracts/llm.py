from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, Sequence


class LLMProviderError(RuntimeError):
    """A sanitized provider failure safe to expose to orchestration code."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ToolSpec:
    """一个可供模型调用的工具；parameters 是 JSON Schema。"""

    name: str
    description: str
    parameters: dict[str, object]

    def wire(self) -> dict[str, object]:
        """Chat Completions 上工具定义的形状；用量估算也用它。

        Responses 协议发的是平铺形状（见 adapters/llm/responses_api.py），比这份短，
        所以那条协议下估算是保守高估（22 个工具全量实测 2.2%），方向安全，不另算一份。
        """
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass(frozen=True)
class ToolCallRequest:
    """模型发起的一次工具调用；arguments 保留原始 JSON 文本，由执行方解析校验。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ServerToolCall:
    """厂商在自己那边执行的一次工具调用（目前只有 server-side 联网搜索）。

    本地没有执行回环：我们既看不到搜索结果，也无从产出可点开的引用，
    拿到的只是「它做了什么」。只用于可观测与回传，不参与任何判断。
    """

    id: str
    kind: str            # web_search
    action: str          # search | open_page | find_in_page
    detail: str          # 查询词或网址，已压成一行
    ok: bool = True
    duration_ms: int = 0
    # 厂商原样的那条记录，回传时照发。只有产生它的适配器认得，别处不要读它的内部结构。
    echo: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None
    # 思考模式下厂商要求把上一轮的思考内容随 assistant 消息回传，不带会被拒。
    reasoning: str = ""
    # 厂商端工具调用要原样回传，它据此恢复自己那边的搜索结果；不回传等于这一轮白搜。
    server_calls: tuple[ServerToolCall, ...] = ()


@dataclass(frozen=True)
class ChatDelta:
    """An incremental piece of answer text emitted while the provider streams."""

    text: str


@dataclass(frozen=True)
class ChatReasoning:
    """思考内容增量。它不是答案的一部分，但要回传给厂商，也用来告诉界面「还在想」。

    field 是厂商实际用的那个字段名（DeepSeek 系 reasoning_content，另一些服务 reasoning），
    只供开发者模式显示。
    """

    text: str
    field: str = "reasoning_content"


@dataclass(frozen=True)
class ChatToolCalls:
    """本次响应以工具调用结束；调用方执行后回填 tool 消息继续对话。"""

    calls: tuple[ToolCallRequest, ...]
    usage: dict[str, int] = field(default_factory=dict)
    reasoning: str = ""
    # 厂商原样返回的 finish_reason，没返回就是 None。纯观测，不参与任何判断。
    provider_finish_reason: str | None = None
    # 这一轮厂商在自己那边跑过的工具调用，见 ServerToolCall。
    server_calls: tuple[ServerToolCall, ...] = ()


@dataclass(frozen=True)
class ChatFinal:
    text: str
    # 上报给客户端的收尾原因。多数时候就是厂商那个值，但服务端也会派生自己的
    # （tool_budget_exhausted、course_unresolved）——要看厂商说了什么请读下面那个。
    finish_reason: str
    provider: str
    model: str
    mode: str
    usage: dict[str, int] = field(default_factory=dict)
    # 厂商原样返回的 finish_reason，没返回就是 None。纯观测，不参与任何判断。
    provider_finish_reason: str | None = None
    # 这一轮厂商在自己那边跑过的工具调用，见 ServerToolCall。
    server_calls: tuple[ServerToolCall, ...] = ()


@dataclass(frozen=True)
class VisionTranscription:
    """架构 §5.7 的 vision_transcription_v1；首版只做整图文字转录，不含坐标与 LaTeX 块。"""

    plain_text: str
    provider: str
    model: str
    needs_confirmation: bool
    schema_version: str = "vision_transcription_v1"
    usage: dict[str, int] = field(default_factory=dict)


class VisionTranscriberPort(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def transcribe(self, *, content: bytes, mime_type: str) -> VisionTranscription:
        """Raise LLMProviderError on provider failure."""
        ...

    def health(self) -> dict[str, object]: ...

    def close(self) -> None: ...


class AgentChatPort(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def chat(self, *, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> Iterable[ChatDelta | ChatReasoning | ChatToolCalls | ChatFinal]:
        """Yield zero or more deltas followed by exactly one ChatToolCalls or ChatFinal.

        Raising LLMProviderError before the first delta means the whole call
        failed; raising after deltas means the stream was interrupted.
        """
        ...

    def health(self) -> dict[str, object]: ...

    def close(self) -> None: ...
