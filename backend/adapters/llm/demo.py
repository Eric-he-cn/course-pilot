from __future__ import annotations

from collections.abc import Iterator, Sequence

from contracts.llm import ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, ToolSpec


class DemoAgentChat:
    """Deterministic local responder used when remote inference is disabled or fails.

    没有工具能力：忽略 tools，从 messages 中已有的工具证据拼出确定性回答。
    """

    @property
    def mode(self) -> str:
        return "demo_fallback"

    @property
    def provider(self) -> str:
        return "local"

    @property
    def model(self) -> str:
        return "evidence-template"

    def chat(self, *, messages: Sequence[ChatMessage], tools: Sequence[ToolSpec] = ()) -> Iterator[ChatDelta | ChatToolCalls | ChatFinal]:
        evidence = [message.content for message in messages if message.role == "tool"]
        useful = [item for item in evidence if item and "未检索到" not in item and not item.startswith("（")]
        if useful:
            joined = "\n\n".join(item[:360] for item in useful[:2])
            text = (
                "[Demo responder] 已在本地资料中检索到相关内容：\n"
                f"{joined}\n\n以上是本地检索证据；可继续追问具体步骤。"
            )
        else:
            text = (
                "[Demo responder] 本地资料库中没有检索到与本问题相关的内容。\n"
                "以下不是当前教材结论：本地演示模式没有通用知识能力。启用远端模型后，"
                "这里会在说明缺少教材依据的前提下给出通用回答。"
            )
        yield ChatDelta(text)
        yield ChatFinal(text=text, finish_reason="demo_fallback", provider=self.provider, model=self.model, mode=self.mode)

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
