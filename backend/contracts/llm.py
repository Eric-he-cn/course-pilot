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
