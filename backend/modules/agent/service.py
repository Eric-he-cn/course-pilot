from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator

from contracts.knowledge import KnowledgeSearchPort, ResolvedKnowledgeScope
from contracts.llm import AgentChatPort, ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, LLMProviderError
from core.common import utc_now
from modules.learning.api import ArchiveReaderPort
from modules.planning.api import PlanReaderPort
from modules.sessions.api import SessionBusyError, SessionUseCases

from .context import SEED_CALL_ID, assemble_messages
from .tools import TOOL_SPECS, CitationRegistry, ToolExecutor
from .trace import TraceWriter


class TurnService:
    """Agent loop：组装历史与种子证据，供模型带工具多轮推进，直到给出最终回答。"""

    def __init__(
        self,
        sessions: SessionUseCases,
        knowledge: KnowledgeSearchPort,
        plans: PlanReaderPort,
        archive: ArchiveReaderPort,
        responder: AgentChatPort,
        fallback_responder: AgentChatPort,
        *,
        trace: TraceWriter | None = None,
        max_tool_rounds: int = 6,
        history_token_budget: int = 128_000,
    ) -> None:
        self._sessions, self._knowledge = sessions, knowledge
        self._responder = responder
        self._fallback_responder = fallback_responder
        self._executor = ToolExecutor(knowledge=knowledge, plans=plans, archive=archive)
        self._trace = trace
        self._max_tool_rounds = max_tool_rounds
        self._history_token_budget = history_token_budget
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    @staticmethod
    def _event(event_name: str, **data: object) -> dict[str, object]:
        return {"event": event_name, "data": data}

    @staticmethod
    def _merge_usage(total: dict[str, int], extra: dict[str, int]) -> None:
        for key, value in extra.items():
            total[key] = total.get(key, 0) + value

    @staticmethod
    def _display_args(raw: str) -> dict:
        try:
            parsed = json.loads(raw) if raw.strip() else {}
            return parsed if isinstance(parsed, dict) else {"raw": raw[:200]}
        except json.JSONDecodeError:
            return {"raw": raw[:200]}

    def run(self, *, session_id: str, message: str, client_request_id: str, attachment_ids: list[str] | None = None) -> Iterator[dict[str, object]]:
        session = self._sessions.get_session(session_id)
        if session is None:
            raise LookupError("会话不存在")
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            yield self._event("turn_failed", error_code="session_busy", retryable=True)
            return
        turn = None
        finalized = False
        started_monotonic = time.monotonic()
        trace_record: dict[str, object] = {"kind": "turn", "started_at": utc_now(), "session_id": session_id, "scope_mode": session.scope_mode}
        trace_tools: list[dict[str, object]] = []
        try:
            if attachment_ids:
                try:
                    attachments = self._sessions.get_attachments(session_id=session_id, attachment_ids=attachment_ids)
                except LookupError:
                    yield self._event("turn_failed", error_code="attachment_not_found", retryable=False)
                    return
                # 转录并入用户消息：检索、提示词与历史记录看到的是同一份内容。
                blocks = "\n\n".join(f"[图片转录：{a.filename}]\n{a.transcription}" for a in attachments)
                message = f"{message}\n\n{blocks}"
            # 历史在写入本轮用户消息之前取，天然不含当前问题。
            history = [(item.role, item.content) for item in self._sessions.list_messages(session_id)]
            turn, created = self._sessions.start_turn(session_id=session_id, client_request_id=client_request_id)
            yield self._event("turn_started", request_id=turn.id, session_id=session_id, scope_mode=session.scope_mode)
            if not created:
                # 重放的 turn 已有终态，不能在 finally 里改写它。
                finalized = True
                yield self._event("turn_completed", message_id=None, finish_reason="idempotent_replay")
                return
            trace_record["turn_id"] = turn.id
            self._sessions.append_message(session_id=session_id, turn_id=turn.id, role="user", content=message)
            context = self._sessions.resolve_turn(turn=turn, message=message)
            trace_record["resolution"] = {"status": context.status, "course_id": context.course_id, "reason": context.reason}
            yield self._event(
                "course_resolution", status=context.status, resolved_course_id=context.course_id, course_id=context.course_id, course_name=context.course_name,
                course_color=context.course_color, reason=context.reason, resolver_version=context.resolver_version,
            )
            registry = CitationRegistry()
            response: ChatFinal | None = None
            answer_parts: list[str] = []
            usage_total: dict[str, int] = {}
            tool_rounds = 0
            seq = 0
            if context.status != "resolved" or context.course_id is None:
                answer = "我还不能确定要使用哪门课程的资料。请在问题中说明课程名称，或先进入具体课程工作区。"
                finish_reason, responder_mode, provider, model = "course_unresolved", "local_guardrail", "system", "none"
                seq += 1
                yield self._event("text_delta", seq=seq, text=answer)
            else:
                scope = ResolvedKnowledgeScope(turn_id=turn.id, course_id=context.course_id, resolver_version=context.resolver_version)
                # 种子检索：先查课程证据是系统行为，不依赖模型自觉；
                # 结果以工具调用的形态注入，模型需要补查时自然复用同一工具。
                seed_args = json.dumps({"query": message}, ensure_ascii=False)
                yield self._event("tool_call", call_id=SEED_CALL_ID, name="search_materials", arguments={"query": message}, origin="seed")
                seed_started = time.monotonic()
                seed = self._executor.execute(scope=scope, name="search_materials", arguments=seed_args, registry=registry)
                for citation in seed.new_citations:
                    yield self._event("citation", **citation)
                yield self._event("tool_result", call_id=SEED_CALL_ID, name="search_materials", ok=seed.ok, summary=seed.summary)
                trace_tools.append({"origin": "seed", "name": "search_materials", "arguments": {"query": message[:200]}, "ok": seed.ok, "summary": seed.summary, "duration_ms": int((time.monotonic() - seed_started) * 1000)})
                messages = assemble_messages(
                    course_name=context.course_name or "当前课程",
                    materials=self._knowledge.material_names(scope=scope),
                    history=history,
                    question=message,
                    seed_query=message,
                    seed_result_text=seed.text,
                    history_token_budget=self._history_token_budget,
                )
                responder = self._responder
                try:
                    while response is None:
                        allow_tools = tool_rounds < self._max_tool_rounds
                        segment_parts: list[str] = []
                        outcome: ChatToolCalls | ChatFinal | None = None
                        for item in responder.chat(messages=messages, tools=TOOL_SPECS if allow_tools else ()):
                            if isinstance(item, ChatDelta):
                                segment_parts.append(item.text)
                                answer_parts.append(item.text)
                                seq += 1
                                yield self._event("text_delta", seq=seq, text=item.text)
                            else:
                                outcome = item
                                break
                        if isinstance(outcome, ChatFinal):
                            response = outcome
                            self._merge_usage(usage_total, outcome.usage)
                        elif isinstance(outcome, ChatToolCalls) and not allow_tools:
                            # 已达工具轮次上限仍尝试调用：以现有内容收尾，保证循环终止。
                            self._merge_usage(usage_total, outcome.usage)
                            response = ChatFinal(
                                text="".join(segment_parts) or "（未能在限定步数内完成检索，请换个问法或稍后重试。）",
                                finish_reason="tool_budget_exhausted", provider=responder.provider,
                                model=responder.model, mode=responder.mode,
                            )
                        elif isinstance(outcome, ChatToolCalls):
                            self._merge_usage(usage_total, outcome.usage)
                            tool_rounds += 1
                            messages.append(ChatMessage(role="assistant", content="".join(segment_parts), tool_calls=outcome.calls))
                            for call in outcome.calls:
                                yield self._event("tool_call", call_id=call.id, name=call.name, arguments=self._display_args(call.arguments), origin="model")
                                call_started = time.monotonic()
                                result = self._executor.execute(scope=scope, name=call.name, arguments=call.arguments, registry=registry)
                                for citation in result.new_citations:
                                    yield self._event("citation", **citation)
                                yield self._event("tool_result", call_id=call.id, name=call.name, ok=result.ok, summary=result.summary)
                                trace_tools.append({"origin": "model", "name": call.name, "arguments": self._display_args(call.arguments), "ok": result.ok, "summary": result.summary, "duration_ms": int((time.monotonic() - call_started) * 1000)})
                                messages.append(ChatMessage(role="tool", content=result.text, tool_call_id=call.id))
                        else:
                            raise LLMProviderError("invalid_response", "供应商流结束但没有终态响应", retryable=False)
                except LLMProviderError as error:
                    if answer_parts:
                        # 已输出增量：保留部分内容并如实标记中断，不静默换供应商重放。
                        yield self._event("stream_interrupted", error_code=error.code, retryable=error.retryable)
                        assistant = self._sessions.append_message(
                            session_id=session_id, turn_id=turn.id, role="assistant",
                            content="".join(answer_parts), citations=registry.citations, status="interrupted",
                        )
                        self._sessions.complete_turn(turn.id, status="failed")
                        finalized = True
                        trace_record.update(status="failed", error_code="stream_interrupted")
                        yield self._event("turn_failed", error_code="stream_interrupted", retryable=False, message_id=assistant.id)
                        return
                    yield self._event(
                        "provider_fallback",
                        provider=self._responder.provider,
                        model=self._responder.model,
                        error_code=error.code,
                        retryable=error.retryable,
                    )
                    for item in self._fallback_responder.chat(messages=messages, tools=()):
                        if isinstance(item, ChatDelta):
                            answer_parts.append(item.text)
                            seq += 1
                            yield self._event("text_delta", seq=seq, text=item.text)
                        elif isinstance(item, ChatFinal):
                            response = item
                            self._merge_usage(usage_total, item.usage)
                            break
                    if response is None:
                        raise LLMProviderError("invalid_response", "本地 responder 没有给出终态响应", retryable=False)
                answer = "".join(answer_parts) or response.text
                finish_reason, responder_mode = response.finish_reason, response.mode
                provider, model = response.provider, response.model
            assistant = self._sessions.append_message(session_id=session_id, turn_id=turn.id, role="assistant", content=answer, citations=registry.citations)
            self._sessions.complete_turn(turn.id, status="completed")
            finalized = True
            trace_record.update(status="completed", answer_chars=len(answer), citations=len(registry.citations), responder={"mode": responder_mode, "provider": provider, "model": model}, usage=usage_total, tool_rounds=tool_rounds)
            yield self._event(
                "turn_completed",
                message_id=assistant.id,
                finish_reason=finish_reason,
                responder_mode=responder_mode,
                provider=provider,
                model=model,
                usage=usage_total,
                tool_rounds=tool_rounds,
            )
        except SessionBusyError:
            yield self._event("turn_failed", error_code="session_busy", retryable=True)
        except Exception:
            trace_record.setdefault("status", "failed")
            yield self._event("turn_failed", error_code="turn_failed", retryable=False)
        finally:
            # finally 对 GeneratorExit（客户端断连）也生效：任何未走到终态的 turn
            # 在这里落为 failed，避免 running 残留把会话永久锁死。
            if turn is not None and not finalized:
                try:
                    self._sessions.complete_turn(turn.id, status="failed")
                except Exception:
                    pass
            if self._trace is not None and trace_record.get("turn_id"):
                trace_record.setdefault("status", "failed" if not finalized else "completed")
                trace_record["tools"] = trace_tools
                trace_record["duration_ms"] = int((time.monotonic() - started_monotonic) * 1000)
                self._trace.write(trace_record)
            lock.release()
