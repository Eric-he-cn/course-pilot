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
        """OpenAI 兼容接口上工具定义的形状。发送与用量估算共用这一份，免得两处各写各的。"""
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


@dataclass(frozen=True)
class ToolCallRequest:
    """模型发起的一次工具调用；arguments 保留原始 JSON 文本，由执行方解析校验。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatMessage:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None
    # 思考模式下厂商要求把上一轮的思考内容随 assistant 消息回传，不带会被拒。
    reasoning: str = ""


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
