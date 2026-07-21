from __future__ import annotations

import threading
from collections.abc import Iterator

from contracts.knowledge import KnowledgeSearchPort, ResolvedKnowledgeScope
from contracts.llm import LLMProviderError, TutorEvidence, TutorRequest, TutorResponderPort, TutorResponse
from modules.sessions.api import SessionBusyError, SessionUseCases


class TurnService:
    """Resolve scope, retrieve evidence and delegate generation through an LLM port."""

    def __init__(
        self,
        sessions: SessionUseCases,
        knowledge: KnowledgeSearchPort,
        responder: TutorResponderPort,
        fallback_responder: TutorResponderPort,
    ) -> None:
        self._sessions, self._knowledge = sessions, knowledge
        self._responder = responder
        self._fallback_responder = fallback_responder
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(session_id, threading.Lock())

    @staticmethod
    def _event(name: str, **data: object) -> dict[str, object]:
        return {"event": name, "data": data}

    def run(self, *, session_id: str, message: str, client_request_id: str) -> Iterator[dict[str, object]]:
        session = self._sessions.get_session(session_id)
        if session is None:
            raise LookupError("会话不存在")
        lock = self._lock_for(session_id)
        if not lock.acquire(blocking=False):
            yield self._event("turn_failed", error_code="session_busy", retryable=True)
            return
        turn = None
        finalized = False
        try:
            turn, created = self._sessions.start_turn(session_id=session_id, client_request_id=client_request_id)
            yield self._event("turn_started", request_id=turn.id, session_id=session_id, scope_mode=session.scope_mode)
            if not created:
                # 重放的 turn 已有终态，不能在 finally 里改写它。
                finalized = True
                yield self._event("turn_completed", message_id=None, finish_reason="idempotent_replay")
                return
            self._sessions.append_message(session_id=session_id, turn_id=turn.id, role="user", content=message)
            context = self._sessions.resolve_turn(turn=turn, message=message)
            yield self._event(
                "course_resolution", status=context.status, resolved_course_id=context.course_id, course_id=context.course_id, course_name=context.course_name,
                course_color=context.course_color, reason=context.reason, resolver_version=context.resolver_version,
            )
            citations: list[dict] = []
            response: TutorResponse | None = None
            if context.status != "resolved" or context.course_id is None:
                answer = "我还不能确定要使用哪门课程的资料。请在问题中说明课程名称，或先进入具体课程工作区。"
                finish_reason, responder_mode, provider, model, usage = "course_unresolved", "local_guardrail", "system", "none", {}
            else:
                hits = self._knowledge.search(
                    scope=ResolvedKnowledgeScope(turn_id=turn.id, course_id=context.course_id, resolver_version=context.resolver_version),
                    query=message,
                    limit=6,
                )
                for index, hit in enumerate(hits, start=1):
                    citation = {
                        "citation_id": f"citation_{index}", "material_id": hit.citation.material_id, "document": hit.citation.document,
                        "page": hit.citation.page, "chunk_id": hit.citation.chunk_id, "snippet": hit.citation.snippet, "score": hit.citation.score,
                    }
                    citations.append(citation)
                    yield self._event("citation", **citation)
                if hits:
                    request = TutorRequest(
                        course_name=context.course_name or "当前课程",
                        question=message,
                        evidence=tuple(
                            TutorEvidence(
                                citation_id=str(index),
                                document=hit.citation.document,
                                page=hit.citation.page,
                                chunk_id=hit.citation.chunk_id,
                                content=hit.content,
                            )
                            for index, hit in enumerate(hits, start=1)
                        ),
                    )
                    try:
                        response = self._responder.respond(request)
                    except LLMProviderError as error:
                        yield self._event(
                            "provider_fallback",
                            provider=self._responder.provider,
                            model=self._responder.model,
                            error_code=error.code,
                            retryable=error.retryable,
                        )
                        response = self._fallback_responder.respond(request)
                    answer = response.text
                    finish_reason, responder_mode = response.finish_reason, response.mode
                    provider, model, usage = response.provider, response.model, response.usage
                else:
                    answer = f"[Demo responder] 已确定课程为“{context.course_name}”，但本地资料库尚未找到可引用的内容。以下不是当前教材结论：请上传或索引相关资料后再检索。"
                    finish_reason, responder_mode, provider, model, usage = "no_evidence", "local_guardrail", "system", "none", {}
            yield self._event("text_delta", seq=1, text=answer)
            assistant = self._sessions.append_message(session_id=session_id, turn_id=turn.id, role="assistant", content=answer, citations=citations)
            self._sessions.complete_turn(turn.id, status="completed")
            finalized = True
            yield self._event(
                "turn_completed",
                message_id=assistant.id,
                finish_reason=finish_reason,
                responder_mode=responder_mode,
                provider=provider,
                model=model,
                usage=usage,
            )
        except SessionBusyError:
            yield self._event("turn_failed", error_code="session_busy", retryable=True)
        except Exception:
            yield self._event("turn_failed", error_code="turn_failed", retryable=False)
        finally:
            # finally 对 GeneratorExit（客户端断连）也生效：任何未走到终态的 turn
            # 在这里落为 failed，避免 running 残留把会话永久锁死。
            if turn is not None and not finalized:
                try:
                    self._sessions.complete_turn(turn.id, status="failed")
                except Exception:
                    pass
            lock.release()
