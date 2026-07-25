from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator

from contracts.knowledge import KnowledgeSearchPort, ResolvedKnowledgeScope
from contracts.llm import AgentChatPort, ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, LLMProviderError, ToolCallRequest
from core.common import utc_now
from modules.learning.api import ArchiveReaderPort, EvidenceWriterPort
from modules.planning.api import PlanReaderPort
from modules.sessions.api import SessionBusyError, SessionUseCases
from modules.sessions.artifacts import ArtifactStore

from .context import PROMPT_VERSION, SEED_CALL_ID, assemble_messages
from .skills import SkillRegistry
from .tools import MAIN_PROFILE, CitationRegistry, ToolExecutor, cited_only, specs_for
from .trace import TraceWriter


# practice 规程要求的副作用：漏一项就意味着这次练习断链（作答不进档案、
# 或者题目没落盘导致下次无法批改）。服务端检查后补一轮提醒，只提醒一次。
_PRACTICE_TODO = {
    "emit_evidence": "为每道可判定的题各调用一次 emit_evidence（答对 attempt_correct，答错或用户说不会 attempt_incorrect，concept_id 取自概念目录）——漏掉这一步，作答不会进入学习档案",
    "artifact_append": "把题目与答案要点写成 artifact（kind=practice 与 kind=practice_key），否则下一轮无法批改这些题",
}


def _practice_reminder(missing: list[str], question_count: int | None) -> str:
    lines = "\n".join(f"- {_PRACTICE_TODO[item]}" for item in missing)
    scope = f"本次练习共 {question_count} 道题。" if question_count else ""
    return f"{scope}你还没有完成 practice 规程要求的这些步骤：\n{lines}\n现在只调用相应工具补上，不要重复输出正文。"


# 明确要练题的说法：命中就直接注入 practice 规程。纯靠模型自觉调 use_skill 会漏，
# 而漏加载意味着这次练习不落 artifact、后续作答也无从批改。误命中的代价很小——
# 规程第一步就是判断本轮该做什么。
_PRACTICE_INTENT = re.compile(r"出\s*(?:几|[一二三四五六1-9])?\s*道|出题|出几题|练练|练习题|做几道|来[一两]道|小测|测测我|考考我|练一练")


# 供应商偶尔会在工具预算耗尽后把 tool_call 当普通文本吐出来，这类内部标记不能进回答。
_PROVIDER_MARKUP = re.compile(r"<[｜|]{1,2}\s*DSML\s*[｜|]{1,2}.*", re.DOTALL)


# 多轮工具调用之间模型爱写"我来查一下""证据齐全了"这类过场话，提示词压不住；
# 短、无引用、无公式、无列表的中间段按过场话丢弃，有实质内容的段落一律保留。
_SUBSTANCE = re.compile(r"\[\d+\]|\$|^\s*[-*\d]", re.MULTILINE)
_FILLER_MAX_CHARS = 80


def _is_filler(segment: str) -> bool:
    text = segment.strip()
    return bool(text) and len(text) <= _FILLER_MAX_CHARS and not _SUBSTANCE.search(text)


def join_answer(segments: list[str]) -> str:
    """最后一段是最终回答，之前的中间段只保留有实质内容的。"""
    if not segments:
        return ""
    kept = [segment for segment in segments[:-1] if segment.strip() and not _is_filler(segment)]
    kept.append(segments[-1])
    return "\n\n".join(part.strip() for part in kept if part.strip())


def _strip_provider_markup(answer: str) -> tuple[str, bool]:
    cleaned = _PROVIDER_MARKUP.sub("", answer).rstrip()
    return (cleaned or answer, cleaned != answer.rstrip())


class TurnService:
    """Agent loop：组装历史与种子证据，供模型带工具多轮推进，直到给出最终回答。"""

    # 心跳写库的最小间隔，需明显小于 SessionService.STALE_TURN_SECONDS。
    HEARTBEAT_SECONDS = 10
    # skill 激活后的工具轮次上限：一次完整评分要读产物、查概念、逐题归因，6 轮不够。
    SKILL_TOOL_ROUNDS = 12

    def __init__(
        self,
        sessions: SessionUseCases,
        knowledge: KnowledgeSearchPort,
        plans: PlanReaderPort,
        archive: ArchiveReaderPort,
        responder: AgentChatPort,
        fallback_responder: AgentChatPort,
        *,
        evidence: EvidenceWriterPort,
        artifacts: ArtifactStore,
        skills: SkillRegistry,
        trace: TraceWriter | None = None,
        max_tool_rounds: int = 6,
        history_token_budget: int = 128_000,
    ) -> None:
        self._sessions, self._knowledge = sessions, knowledge
        self._responder = responder
        self._fallback_responder = fallback_responder
        self._skills = skills
        self._artifacts = artifacts
        self._executor = ToolExecutor(
            knowledge=knowledge, plans=plans, archive=archive,
            evidence=evidence, artifacts=artifacts, skills=skills,
        )
        self._trace = trace
        self._max_tool_rounds = max_tool_rounds
        self._history_token_budget = history_token_budget

    @staticmethod
    def _event(event_name: str, **data: object) -> dict[str, object]:
        return {"event": event_name, "data": data}

    def _heartbeat(self, turn_id: str, last: float) -> float:
        """按 HEARTBEAT_SECONDS 节流续约：证明这一轮还活着，避免被下一轮当成失活抢占。"""
        now = time.monotonic()
        if now - last < self.HEARTBEAT_SECONDS:
            return last
        try: self._sessions.touch_turn(turn_id)
        except Exception: pass  # 续约失败不该打断对话，最坏是本轮被后来者接管
        return now

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
        turn = None
        finalized = False
        started_monotonic = time.monotonic()
        trace_record: dict[str, object] = {"kind": "turn", "started_at": utc_now(), "session_id": session_id, "scope_mode": session.scope_mode, "prompt_version": PROMPT_VERSION}
        trace_tools: list[dict[str, object]] = []
        # 面向用户的工具活动，与消息一同持久化，刷新后仍能看到本轮查了什么。
        activity: list[dict[str, object]] = []
        last_heartbeat = time.monotonic()
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
            answer_segments: list[str] = []
            usage_total: dict[str, int] = {}
            tool_rounds = 0
            seq = 0
            if context.status != "resolved" or context.course_id is None:
                if context.candidates:
                    answer = "你的问题同时提到了" + "、".join(f"「{name}」" for name in context.candidates) + "，我不确定该用哪一门的资料。说明是哪一门，我就接着答。"
                else:
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
                seed = self._executor.execute(scope=scope, session_id=session_id, name="search_materials", arguments=seed_args, registry=registry, allowed=MAIN_PROFILE)
                for citation in seed.new_citations:
                    yield self._event("citation", **citation)
                yield self._event("tool_result", call_id=SEED_CALL_ID, name="search_materials", ok=seed.ok, summary=seed.summary)
                activity.append({"call_id": SEED_CALL_ID, "name": "search_materials", "origin": "seed", "ok": seed.ok, "summary": seed.summary})
                trace_tools.append({"origin": "seed", "name": "search_materials", "arguments": {"query": message[:200]}, "ok": seed.ok, "summary": seed.summary, "duration_ms": int((time.monotonic() - seed_started) * 1000)})
                messages = assemble_messages(
                    course_name=context.course_name or "当前课程",
                    materials=self._knowledge.material_names(scope=scope),
                    history=history,
                    question=message,
                    seed_query=message,
                    seed_result_text=seed.text,
                    history_token_budget=self._history_token_budget,
                    skill_summaries=self._skills.summaries(),
                    practice_digest=self._artifacts.practice_digest(session_id=session_id),
                )
                responder = self._responder
                allowed_tools = MAIN_PROFILE
                active_skill: str | None = None
                max_rounds = self._max_tool_rounds
                # 有尚未批改的练习时直接把 practice 规程注入：纯靠模型自觉加载 skill 会漏，
                # 而漏批改意味着这次作答不进学习档案。加载后仍由规程自己判断本轮做什么。
                pending = self._artifacts.latest_practice(session_id=session_id)
                practice_skill = self._skills.get("practice")
                awaiting_grade = pending is not None and not pending[2]
                evidence_emitted = False
                artifact_written = False
                practice_reminded = False
                wants_practice = bool(_PRACTICE_INTENT.search(message))
                if practice_skill is not None and (awaiting_grade or wants_practice):
                    call_id = "call_auto_practice"
                    yield self._event("tool_call", call_id=call_id, name="use_skill", arguments={"name": "practice"}, origin="auto")
                    reason = f"练习 {pending[0]} 待批改" if awaiting_grade else "用户要练题"
                    yield self._event("tool_result", call_id=call_id, name="use_skill", ok=True, summary=f"自动加载 practice（{reason}）")
                    activity.append({"call_id": call_id, "name": "use_skill", "origin": "auto", "ok": True, "summary": "自动加载 practice"})
                    trace_tools.append({"origin": "auto", "name": "use_skill", "arguments": {"name": "practice"}, "ok": True, "summary": "自动加载", "duration_ms": 0})
                    messages.append(ChatMessage(role="assistant", content="", tool_calls=(ToolCallRequest(id=call_id, name="use_skill", arguments=json.dumps({"name": "practice"})),)))
                    messages.append(ChatMessage(role="tool", content=f"# Skill: practice\n\n{practice_skill.body}", tool_call_id=call_id))
                    active_skill, allowed_tools = "practice", practice_skill.allowed_tools
                    max_rounds = max(max_rounds, self.SKILL_TOOL_ROUNDS)
                    trace_record["skill"] = {"name": "practice", "content_hash": practice_skill.content_hash, "activation": "auto"}
                try:
                    while response is None:
                        allow_tools = tool_rounds < max_rounds
                        segment_parts: list[str] = []
                        outcome: ChatToolCalls | ChatFinal | None = None
                        for item in responder.chat(messages=messages, tools=specs_for(allowed_tools) if allow_tools else ()):
                            if isinstance(item, ChatDelta):
                                segment_parts.append(item.text)
                                answer_parts.append(item.text)
                                seq += 1
                                last_heartbeat = self._heartbeat(turn.id, last_heartbeat)
                                yield self._event("text_delta", seq=seq, text=item.text)
                            else:
                                outcome = item
                                break
                        if isinstance(outcome, ChatFinal):
                            answer_segments.append("".join(segment_parts))
                            missing_steps = []
                            if active_skill == "practice" and not practice_reminded and tool_rounds < max_rounds:
                                if awaiting_grade and not evidence_emitted:
                                    missing_steps.append("emit_evidence")
                                if (wants_practice or awaiting_grade) and not artifact_written:
                                    missing_steps.append("artifact_append")
                            if missing_steps:
                                # 规程有步骤没做完就补一轮，只补一次。
                                practice_reminded = True
                                self._merge_usage(usage_total, outcome.usage)
                                messages.append(ChatMessage(role="assistant", content="".join(segment_parts)))
                                messages.append(ChatMessage(role="user", content=_practice_reminder(missing_steps, pending[1] if pending else None)))
                                trace_record["practice_reminder"] = missing_steps
                                continue
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
                            answer_segments.append("".join(segment_parts))
                            messages.append(ChatMessage(role="assistant", content="".join(segment_parts), tool_calls=outcome.calls))
                            for call in outcome.calls:
                                yield self._event("tool_call", call_id=call.id, name=call.name, arguments=self._display_args(call.arguments), origin="model")
                                call_started = time.monotonic()
                                result = self._executor.execute(scope=scope, session_id=session_id, name=call.name, arguments=call.arguments, registry=registry, allowed=allowed_tools)
                                if call.name == "emit_evidence" and result.ok:
                                    evidence_emitted = True
                                if call.name == "artifact_append" and result.ok:
                                    artifact_written = True
                                if result.activated_skill and active_skill is None:
                                    # 一轮只激活一个前台 skill，激活后工具集收窄到它声明的范围。
                                    active_skill = result.activated_skill
                                    skill = self._skills.get(active_skill)
                                    allowed_tools = skill.allowed_tools if skill else allowed_tools
                                    max_rounds = max(max_rounds, self.SKILL_TOOL_ROUNDS)
                                    trace_record["skill"] = {"name": active_skill, "content_hash": skill.content_hash if skill else None, "activation": "model"}
                                for citation in result.new_citations:
                                    yield self._event("citation", **citation)
                                yield self._event("tool_result", call_id=call.id, name=call.name, ok=result.ok, summary=result.summary)
                                activity.append({"call_id": call.id, "name": call.name, "origin": "model", "ok": result.ok, "summary": result.summary})
                                trace_tools.append({"origin": "model", "name": call.name, "arguments": self._display_args(call.arguments), "ok": result.ok, "summary": result.summary, "duration_ms": int((time.monotonic() - call_started) * 1000)})
                                messages.append(ChatMessage(role="tool", content=result.text, tool_call_id=call.id))
                        else:
                            raise LLMProviderError("invalid_response", "供应商流结束但没有终态响应", retryable=False)
                except LLMProviderError as error:
                    if answer_parts:
                        # 已输出增量：保留部分内容并如实标记中断，不静默换供应商重放。
                        yield self._event("stream_interrupted", error_code=error.code, retryable=error.retryable)
                        partial = "".join(answer_parts)
                        assistant = self._sessions.append_message(
                            session_id=session_id, turn_id=turn.id, role="assistant",
                            content=partial, citations=cited_only(partial, registry.citations), status="interrupted", activity=activity,
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
                answer, leaked = _strip_provider_markup(join_answer(answer_segments) or "".join(answer_parts) or response.text)
                if leaked:
                    trace_record["provider_markup_stripped"] = True
                finish_reason, responder_mode = response.finish_reason, response.mode
                provider, model = response.provider, response.model
            if not self._sessions.touch_turn(turn.id):
                # 本轮已被判失活、会话被新一轮接管，不再写回答，避免消息错乱。
                finalized = True
                trace_record.update(status="failed", error_code="turn_superseded")
                yield self._event("turn_failed", error_code="turn_superseded", retryable=False)
                return
            citations = cited_only(answer, registry.citations)
            assistant = self._sessions.append_message(session_id=session_id, turn_id=turn.id, role="assistant", content=answer, citations=citations, activity=activity)
            self._sessions.complete_turn(turn.id, status="completed")
            finalized = True
            trace_record.update(status="completed", answer_chars=len(answer), citations=len(citations), citations_retrieved=len(registry.citations), responder={"mode": responder_mode, "provider": provider, "model": model}, usage=usage_total, tool_rounds=tool_rounds)
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
            trace_record.update(status="failed", error_code="session_busy")
            yield self._event("turn_failed", error_code="session_busy", retryable=True)
        except Exception as error:
            trace_record.setdefault("status", "failed")
            trace_record.setdefault("error_code", f"unhandled:{type(error).__name__}")
            yield self._event("turn_failed", error_code="turn_failed", retryable=False)
        finally:
            # 正常路径靠这里收尾；客户端断连时生成器可能一直挂在 yield 上不进 finally，
            # 那种失活 turn 由心跳超时让下一轮接管。
            if turn is not None and not finalized:
                try:
                    self._sessions.complete_turn(turn.id, status="failed")
                except Exception:
                    pass
            if self._trace is not None and trace_record.get("turn_id"):
                trace_record.setdefault("status", "failed" if not finalized else "completed")
                if trace_record["status"] == "failed":
                    # 未走到终态多半是客户端断连；排查时要能看出失败原因而不是只有 failed。
                    trace_record.setdefault("error_code", "client_disconnected")
                trace_record["tools"] = trace_tools
                trace_record["duration_ms"] = int((time.monotonic() - started_monotonic) * 1000)
                self._trace.write(trace_record)
