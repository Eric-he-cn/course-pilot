from __future__ import annotations

from collections.abc import Iterator

from contracts.llm import TutorDelta, TutorRequest, TutorResponse


class DemoTutorResponder:
    """Deterministic local responder used when remote inference is disabled or fails."""

    @property
    def mode(self) -> str:
        return "demo_fallback"

    @property
    def provider(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return "evidence-template"

    def respond(self, request: TutorRequest) -> Iterator[TutorDelta | TutorResponse]:
        evidence = "\n\n".join(f"- [{item.citation_id}] {item.content[:360]}" for item in request.evidence[:3])
        text = (
            f"[Demo responder] 已在“{request.course_name}”的本地资料中检索到相关内容：\n"
            f"{evidence}\n\n以上是本地检索证据；可继续追问具体步骤。"
        )
        yield TutorDelta(text)
        yield TutorResponse(
            text=text,
            finish_reason="demo_fallback",
            provider=self.provider,
            model=self.model,
            mode=self.mode,
        )

    def health(self) -> dict[str, object]:
        return {
            "configured": True,
            "enabled": True,
            "mode": self.mode,
            "adapter_available": True,
            "provider": self.provider,
            "model": self.model,
        }

    def close(self) -> None:
        return None
