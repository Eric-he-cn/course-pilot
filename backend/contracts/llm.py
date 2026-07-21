from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol


class LLMProviderError(RuntimeError):
    """A sanitized provider failure safe to expose to orchestration code."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class TutorEvidence:
    citation_id: str
    document: str
    page: int | None
    chunk_id: str
    content: str


@dataclass(frozen=True)
class TutorRequest:
    course_name: str
    question: str
    evidence: tuple[TutorEvidence, ...]
    # 资料库文件名清单：让模型知道课程里有什么教材，能回答"有没有 X 资料"。
    materials: tuple[str, ...] = ()


@dataclass(frozen=True)
class TutorDelta:
    """An incremental piece of answer text emitted while the provider streams."""

    text: str


@dataclass(frozen=True)
class TutorResponse:
    text: str
    finish_reason: str
    provider: str
    model: str
    mode: str
    usage: dict[str, int] = field(default_factory=dict)


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


class TutorResponderPort(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def respond(self, request: TutorRequest) -> Iterable[TutorDelta | TutorResponse]:
        """Yield zero or more deltas followed by exactly one terminal TutorResponse.

        Raising LLMProviderError before the first delta means the whole call
        failed; raising after deltas means the stream was interrupted.
        """
        ...

    def health(self) -> dict[str, object]: ...

    def close(self) -> None: ...
