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
        if request.evidence:
            evidence = "\n\n".join(f"- [{item.citation_id}] {item.content[:360]}" for item in request.evidence[:3])
            text = (
                f"[Demo responder] 已在“{request.course_name}”的本地资料中检索到相关内容：\n"
                f"{evidence}\n\n以上是本地检索证据；可继续追问具体步骤。"
            )
        else:
            library = f"资料库中已有：{'、'.join(request.materials)}，但本问题未命中其内容。" if request.materials else "该课程还没有上传教材。"
            text = (
                f"[Demo responder] 在“{request.course_name}”的资料库中没有检索到与本问题相关的内容。{library}\n"
                "以下不是当前教材结论：本地演示模式没有通用知识能力。启用远端模型后，"
                "这里会在说明缺少教材依据的前提下给出通用回答。"
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
