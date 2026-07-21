from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


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


@dataclass(frozen=True)
class TutorResponse:
    text: str
    finish_reason: str
    provider: str
    model: str
    mode: str
    usage: dict[str, int] = field(default_factory=dict)


class TutorResponderPort(Protocol):
    @property
    def mode(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def respond(self, request: TutorRequest) -> TutorResponse: ...

    def health(self) -> dict[str, object]: ...

    def close(self) -> None: ...
