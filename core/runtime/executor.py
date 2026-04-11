"""ExecutionRuntime: compile and execute TaskGraph against the existing runner."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Tuple

from core.metrics import add_event
from core.runtime.taskgraph import TaskGraphStepV1, TaskGraphV1

if TYPE_CHECKING:
    from backend.schemas import AgentContextV1, ChatMessage, PlanPlusV1, SessionStateV1
    from core.orchestration.runner import OrchestrationRunner


class ExecutionRuntime:
    """Thin runtime wrapper around the existing mode-specific runner methods.

    v3 first stage goal:
    - compile an explicit TaskGraph
    - persist its digest into SessionState
    - route all requests through a single runtime entry
    - reuse stable mode implementations to avoid a rewrite cliff
    """

    def __init__(self, runner: "OrchestrationRunner") -> None:
        self.runner = runner

    def _prepare_request(
        self,
        *,
        course_name: str,
        mode_hint: str,
        user_message: str,
        state: Dict[str, Any] | None,
        history: List[Dict[str, str]] | None,
    ) -> Tuple["PlanPlusV1", Dict[str, Any], "SessionStateV1", str, List[Dict[str, str]]]:
        history_list = history or []
        runtime_state = dict(state or {})
        session_state = self.runner._extract_session_state(
            history=history_list,
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            state=runtime_state,
        )
        plan = self.runner.router.plan(
            user_message,
            mode_hint,
            course_name,
            session_state=session_state,
        )
        resolved_mode = str(getattr(plan, "resolved_mode", "") or mode_hint)
        if resolved_mode != mode_hint:
            add_event(
                "mode_override",
                session_id=session_state.session_id,
                mode_hint=mode_hint,
                resolved_mode=resolved_mode,
            )
        session_state = self.runner._update_session_state(
            session_state,
            requested_mode_hint=mode_hint,
            resolved_mode=resolved_mode,
            task_full_text=user_message,
            task_summary=self.runner._summarize_task_text(user_message),
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            user_intent=str(getattr(plan, "user_intent", "") or user_message),
            retrieval_query=str(getattr(plan, "retrieval_query", "") or user_message),
            memory_query=str(getattr(plan, "memory_query", "") or user_message),
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="router_planned",
            current_step_index=0,
        )
        runtime_state["session_id"] = session_state.session_id
        runtime_state["session_state"] = session_state
        return plan, runtime_state, session_state, resolved_mode, history_list

    def _compile_route(
        self,
        *,
        resolved_mode: str,
        user_message: str,
        history: List[Dict[str, str]],
    ) -> Tuple[str, Dict[str, Any]]:
        metadata: Dict[str, Any] = {}
        if resolved_mode == "learn":
            return "run_tutor", metadata
        if resolved_mode == "practice":
            answer_submission = self.runner._is_answer_submission(user_message, history)
            metadata["answer_submission"] = answer_submission
            if answer_submission:
                return "run_grade", metadata
            _, _, num_questions, question_type = self.runner._resolve_quiz_request(user_message)
            metadata["num_questions"] = num_questions
            metadata["question_type"] = question_type
            if num_questions > 1:
                return "run_exam", metadata
            return "run_quiz", metadata
        answer_submission = self.runner._is_exam_answer_submission(user_message, history)
        metadata["answer_submission"] = answer_submission
        if answer_submission:
            return "run_grade", metadata
        return "run_exam", metadata

    @staticmethod
    def _graph_digest(graph: TaskGraphV1) -> str:
        payload = {
            "session_id": graph.session_id,
            "mode_hint": graph.mode_hint,
            "resolved_mode": graph.resolved_mode,
            "route": graph.route,
            "steps": graph.step_names(),
            "metadata": graph.metadata,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _strict_runtime() -> bool:
        return os.getenv("STRICT_NEW_RUNTIME", "0") == "1"

    @staticmethod
    def _step(graph: TaskGraphV1, name: str) -> TaskGraphStepV1 | None:
        for step in graph.steps:
            if step.step_name == name:
                return step
        return None

    def _mark_step(self, graph: TaskGraphV1, name: str, status: str, **metadata: Any) -> None:
        step = self._step(graph, name)
        if step is None:
            return
        step.status = status  # type: ignore[assignment]
        if metadata:
            step.metadata.update(metadata)

    def _refresh_session_state_from_graph(self, runtime_state: Dict[str, Any], graph: TaskGraphV1) -> None:
        session_state = runtime_state["session_state"]
        status_map = {step.step_name: step.status for step in graph.steps}
        fallback_events = list(session_state.metadata.get("fallback_events", []))
        runtime_state["session_state"] = self.runner._update_session_state(
            session_state,
            last_taskgraph_digest=str(graph.metadata.get("digest", "")),
            metadata={
                **session_state.metadata,
                "taskgraph_route": graph.route,
                "taskgraph_steps": graph.step_names(),
                "taskgraph_statuses": status_map,
                "fallback_events": fallback_events,
            },
        )

    def _reload_session_state(self, course_name: str, runtime_state: Dict[str, Any]) -> None:
        session_id = str(runtime_state["session_state"].session_id)
        try:
            stored = self.runner.workspace_store.load_session_state(course_name, session_id)
        except Exception:
            stored = None
        if stored is not None:
            runtime_state["session_state"] = stored

    @staticmethod
    def _install_runtime_controls(runtime_state: Dict[str, Any], graph: TaskGraphV1) -> None:
        runtime_state["_runtime_managed"] = True
        runtime_state["_runtime_route"] = graph.route
        runtime_state.setdefault("_runtime_effects", {})

    @staticmethod
    def _clear_runtime_controls(runtime_state: Dict[str, Any]) -> None:
        runtime_state.pop("_runtime_managed", None)
        runtime_state.pop("_runtime_route", None)
        runtime_state.pop("_runtime_effects", None)

    def _persist_runtime_effects(
        self,
        *,
        course_name: str,
        runtime_state: Dict[str, Any],
    ) -> str:
        effects = runtime_state.get("_runtime_effects")
        if not isinstance(effects, dict):
            return ""
        notice = ""
        record_payload = effects.get("persist_records")
        if isinstance(record_payload, dict):
            kind = str(record_payload.get("kind", "") or "")
            if kind == "practice_record":
                saved_path = self.runner._save_practice_record(
                    str(record_payload.get("course_name", "") or course_name),
                    str(record_payload.get("user_message", "") or ""),
                    list(record_payload.get("history", []) or []),
                    str(record_payload.get("response_text", "") or ""),
                )
                notice = f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"
            elif kind == "exam_record":
                saved_path = self.runner._save_exam_record(
                    str(record_payload.get("course_name", "") or course_name),
                    str(record_payload.get("user_message", "") or ""),
                    list(record_payload.get("history", []) or []),
                    str(record_payload.get("response_text", "") or ""),
                )
                notice = f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"

        memory_payload = effects.get("persist_memory")
        if isinstance(memory_payload, dict):
            kind = str(memory_payload.get("kind", "") or "")
            if kind == "learn_episode":
                self.runner.memory_service.save_learn_episode(
                    str(memory_payload.get("course_name", "") or course_name),
                    str(memory_payload.get("question_raw", "") or ""),
                    list(memory_payload.get("doc_ids", []) or []),
                )
            elif kind == "practice_grade":
                self.runner.memory_service.save_practice_grade(
                    str(memory_payload.get("course_name", "") or course_name),
                    str(memory_payload.get("user_answer", "") or ""),
                    list(memory_payload.get("history", []) or []),
                    str(memory_payload.get("response_text", "") or ""),
                )
            elif kind == "exam_grade":
                self.runner.memory_service.save_exam_grade(
                    str(memory_payload.get("course_name", "") or course_name),
                    str(memory_payload.get("response_text", "") or ""),
                )
        return notice

    @staticmethod
    def _carry_visible_internal_tool_calls(tool_calls: Any) -> List[Dict[str, Any]]:
        carried: List[Dict[str, Any]] = []
        for item in list(tool_calls or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") != "internal_meta":
                continue
            name = str(item.get("name", "") or "")
            if name in {"session_state", "history_summary_state"}:
                continue
            carried.append(item)
        return carried

    def _build_agent_context(
        self,
        *,
        course_name: str,
        resolved_mode: str,
        user_message: str,
        history: List[Dict[str, str]],
        plan: "PlanPlusV1",
        runtime_state: Dict[str, Any],
    ) -> "AgentContextV1":
        session_state = runtime_state["session_state"]
        retrieval_query = self.runner._plan_retrieval_query(plan, user_message)
        memory_query = self.runner._plan_memory_query(plan, user_message)
        history_summary_state, pending_history, recent_history, history_metrics = self.runner._prepare_history_summary_inputs(history)
        runtime_state["session_state"] = self.runner._update_session_state(
            session_state,
            history_summary_state=history_summary_state,
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            user_intent=str(getattr(plan, "user_intent", "") or user_message),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
        )
        mem_agent = "tutor"
        mem_phase = "answer"
        if resolved_mode in {"practice", "exam"}:
            route_meta = dict(getattr(runtime_state.get("graph"), "metadata", {}).get("route_meta", {}) or {})
            answer_submission = bool(route_meta.get("answer_submission", False))
            mem_agent = "grader" if answer_submission else "quizzer"
            mem_phase = "grade" if answer_submission else "generate"

        with ThreadPoolExecutor(max_workers=2) as pool:
            rag_future = pool.submit(
                self.runner.rag_service.retrieve,
                course_name=course_name,
                retrieval_query=retrieval_query,
                mode=resolved_mode,
                need_rag=bool(getattr(plan, "need_rag", True)),
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于已有上下文继续）",
            )
            mem_future = pool.submit(
                self.runner.memory_service.prefetch_history_ctx,
                query=memory_query,
                course_name=course_name,
                mode=resolved_mode,
                agent=mem_agent,
                phase=mem_phase,
            )
            rag_text, citations, retrieval_empty = rag_future.result()
            memory_ctx = mem_future.result()

        agent_context, packed, _ = self.runner._build_agent_context(
            session_state=runtime_state["session_state"],
            history=history,
            retrieval_query=retrieval_query,
            rag_text=rag_text,
            memory_text=memory_ctx,
            citations=citations,
            mode=resolved_mode,
            history_summary_state=history_summary_state,
            pending_history=pending_history,
            recent_history=recent_history,
            history_metrics=history_metrics,
        )
        agent_context.metadata["retrieval_empty"] = retrieval_empty
        agent_context.metadata["context_budget"] = self.runner._context_budget_payload(resolved_mode, len(history), packed)
        return agent_context

    def _fallback_to_legacy_sync(
        self,
        *,
        course_name: str,
        user_message: str,
        plan: "PlanPlusV1",
        resolved_mode: str,
        runtime_state: Dict[str, Any],
        history: List[Dict[str, str]],
        graph: TaskGraphV1,
        reason: str,
    ) -> "ChatMessage":
        if self._strict_runtime():
            raise RuntimeError(reason)
        session_state = runtime_state["session_state"]
        fallback_event = {
            "reason": reason,
            "from_path": "new_runtime",
            "to_path": "compat_mode",
            "route": graph.route,
        }
        self.runner.telemetry_service.record_fallback(
            session_id=session_state.session_id,
            request_id=str(session_state.metadata.get("request_id", "")),
            agent="runtime",
            step_name="dispatch",
            reason=reason,
            from_path="new_runtime",
            to_path="compat_mode",
        )
        runtime_state["session_state"] = self.runner._update_session_state(
            session_state,
            fallback_flags=list(session_state.fallback_flags) + [reason],
            metadata={
                **session_state.metadata,
                "fallback_events": list(session_state.metadata.get("fallback_events", [])) + [fallback_event],
            },
        )
        runtime_state.pop("agent_context", None)
        self._clear_runtime_controls(runtime_state)
        self._refresh_session_state_from_graph(runtime_state, graph)
        return self._dispatch_sync(
            course_name=course_name,
            user_message=user_message,
            plan=plan,
            resolved_mode=resolved_mode,
            runtime_state=runtime_state,
            history=history,
        )

    def compile_taskgraph(
        self,
        *,
        course_name: str,
        mode_hint: str,
        user_message: str,
        history: List[Dict[str, str]],
        plan: "PlanPlusV1",
        session_state: "SessionStateV1",
        stream: bool,
    ) -> TaskGraphV1:
        resolved_mode = str(getattr(plan, "resolved_mode", "") or mode_hint)
        route, route_meta = self._compile_route(
            resolved_mode=resolved_mode,
            user_message=user_message,
            history=history,
        )

        steps: List[TaskGraphStepV1] = [
            TaskGraphStepV1(step_name="plan_intent", phase="plan", stream=stream),
        ]
        if plan.need_rag:
            steps.append(TaskGraphStepV1(step_name="prefetch_rag", phase="prefetch", stream=stream))
        if getattr(plan, "need_memory", True):
            steps.append(TaskGraphStepV1(step_name="prefetch_memory", phase="prefetch", stream=stream))
        steps.append(TaskGraphStepV1(step_name="build_agent_context", phase="context", stream=stream))

        if resolved_mode in {"practice", "exam"}:
            steps.append(
                TaskGraphStepV1(
                    step_name="detect_submission",
                    phase="route",
                    stream=stream,
                    metadata={"resolved_mode": resolved_mode, **route_meta},
                )
            )

        if route == "run_tutor":
            steps.append(TaskGraphStepV1(step_name="run_tutor", phase="execute", stream=stream))
        elif route == "run_quiz":
            steps.append(TaskGraphStepV1(step_name="run_quiz", phase="execute", stream=stream))
        elif route == "run_exam":
            steps.append(TaskGraphStepV1(step_name="run_exam", phase="execute", stream=stream))
        else:
            steps.append(
                TaskGraphStepV1(
                    step_name="run_grade",
                    phase="execute",
                    side_effect=True,
                    stream=stream,
                    metadata={"resolved_mode": resolved_mode, **route_meta},
                )
            )

        steps.append(TaskGraphStepV1(step_name="persist_session_state", phase="persist", side_effect=True, stream=stream))
        if route in {"run_grade"}:
            steps.append(TaskGraphStepV1(step_name="persist_records", phase="persist", side_effect=True, stream=stream))
            steps.append(TaskGraphStepV1(step_name="persist_memory", phase="persist", side_effect=True, stream=stream))
        steps.append(TaskGraphStepV1(step_name="synthesize_final", phase="finalize", stream=stream))

        graph = TaskGraphV1(
            graph_id=uuid.uuid4().hex,
            session_id=session_state.session_id,
            mode_hint=mode_hint,  # type: ignore[arg-type]
            resolved_mode=resolved_mode,  # type: ignore[arg-type]
            route=route,  # type: ignore[arg-type]
            steps=steps,
            metadata={
                "course_name": course_name,
                "stream": stream,
                "route_meta": route_meta,
            },
        )
        digest = self._graph_digest(graph)
        graph.metadata["digest"] = digest
        return graph

    def _dispatch_sync(
        self,
        *,
        course_name: str,
        user_message: str,
        plan: "PlanPlusV1",
        resolved_mode: str,
        runtime_state: Dict[str, Any],
        history: List[Dict[str, str]],
    ) -> "ChatMessage":
        if resolved_mode == "learn":
            return self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)
        if resolved_mode == "practice":
            return self.runner.run_practice_mode(course_name, user_message, plan, runtime_state, history)
        if resolved_mode == "exam":
            return self.runner.run_exam_mode(course_name, user_message, plan, history, state=runtime_state)
        return self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)

    def _dispatch_stream(
        self,
        *,
        course_name: str,
        user_message: str,
        plan: "PlanPlusV1",
        resolved_mode: str,
        runtime_state: Dict[str, Any],
        history: List[Dict[str, str]],
    ) -> Iterator[Any]:
        if resolved_mode == "learn":
            yield from self.runner.run_learn_mode_stream(course_name, user_message, plan, history, state=runtime_state)
            return
        if resolved_mode == "practice":
            yield from self.runner.run_practice_mode_stream(course_name, user_message, plan, history, state=runtime_state)
            return
        if resolved_mode == "exam":
            yield from self.runner.run_exam_mode_stream(course_name, user_message, plan, history, state=runtime_state)
            return
        response = self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)
        yield response.content

    def execute_sync(
        self,
        *,
        course_name: str,
        mode_hint: str,
        user_message: str,
        state: Dict[str, Any] | None,
        history: List[Dict[str, str]] | None,
    ) -> Tuple["ChatMessage", "PlanPlusV1", TaskGraphV1]:
        plan, runtime_state, session_state, resolved_mode, history_list = self._prepare_request(
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            state=state,
            history=history,
        )

        graph = self.compile_taskgraph(
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            history=history_list,
            plan=plan,
            session_state=session_state,
            stream=False,
        )
        runtime_state["graph"] = graph
        self._install_runtime_controls(runtime_state, graph)
        self._mark_step(graph, "plan_intent", "completed")
        runtime_state["session_state"] = self.runner._update_session_state(
            runtime_state["session_state"],
            current_stage="runtime_compiled",
        )
        self._refresh_session_state_from_graph(runtime_state, graph)
        add_event(
            "taskgraph_compiled",
            graph_id=graph.graph_id,
            session_id=graph.session_id,
            resolved_mode=resolved_mode,
            route=graph.route,
            step_count=len(graph.steps),
            digest=str(graph.metadata.get("digest", "")),
            stream=False,
        )
        try:
            if self._step(graph, "prefetch_rag") is not None:
                self._mark_step(graph, "prefetch_rag", "completed")
            if self._step(graph, "prefetch_memory") is not None:
                self._mark_step(graph, "prefetch_memory", "completed")
            agent_context = self._build_agent_context(
                course_name=course_name,
                resolved_mode=resolved_mode,
                user_message=user_message,
                history=history_list,
                plan=plan,
                runtime_state=runtime_state,
            )
            runtime_state["agent_context"] = agent_context
            self._mark_step(graph, "build_agent_context", "completed")
            if self._step(graph, "detect_submission") is not None:
                self._mark_step(graph, "detect_submission", "completed", **graph.metadata.get("route_meta", {}))
            self._refresh_session_state_from_graph(runtime_state, graph)
            response = self._dispatch_sync(
                course_name=course_name,
                user_message=user_message,
                plan=plan,
                resolved_mode=resolved_mode,
                runtime_state=runtime_state,
                history=history_list,
            )
            notice = self._persist_runtime_effects(course_name=course_name, runtime_state=runtime_state)
            if notice:
                response = response.model_copy(update={"content": f"{response.content}{notice}"})
        except Exception as exc:
            response = self._fallback_to_legacy_sync(
                course_name=course_name,
                user_message=user_message,
                plan=plan,
                resolved_mode=resolved_mode,
                runtime_state=runtime_state,
                history=history_list,
                graph=graph,
                reason=f"runtime_step_failed:{type(exc).__name__}",
            )
            self._reload_session_state(course_name, runtime_state)
        self._mark_step(graph, graph.route, "completed")
        self._mark_step(graph, "persist_session_state", "completed")
        if graph.route == "run_grade":
            self._mark_step(graph, "persist_records", "completed")
            self._mark_step(graph, "persist_memory", "completed")
        self._mark_step(graph, "synthesize_final", "completed")
        self._refresh_session_state_from_graph(runtime_state, graph)
        self.runner._persist_session_state(runtime_state["session_state"])
        response = response.model_copy(
            update={
                "tool_calls": self.runner._final_internal_tool_calls(
                    session_state=runtime_state["session_state"],
                    history_summary_state=dict(runtime_state["session_state"].history_summary_state or {}),
                    extra_tool_calls=self._carry_visible_internal_tool_calls(response.tool_calls)
                    + list((runtime_state.get("_runtime_effects", {}) or {}).get("final_tool_calls", []) or []),
                )
            }
        )
        self._reload_session_state(course_name, runtime_state)
        self._clear_runtime_controls(runtime_state)

        enable_replan = os.getenv("ENABLE_ROUTER_REPLAN", "1") == "1"
        if enable_replan and graph.route != "run_grade":
            reasons = self.runner._collect_replan_reasons(resolved_mode, plan, response)
            if reasons:
                reason_text = "；".join(reasons)
                self.runner.logger.info("[replan] trigger=1 mode=%s reasons=%s%s", resolved_mode, reason_text, self.runner._trace_tag())
                new_plan = self.runner.router.replan(
                    user_message=user_message,
                    mode=mode_hint,
                    course_name=course_name,
                    previous_plan=plan,
                    reason=reason_text,
                )
                if new_plan.model_dump() != plan.model_dump():
                    plan = new_plan
                    resolved_mode = str(getattr(plan, "resolved_mode", "") or mode_hint)
                    runtime_state["session_state"] = self.runner._update_session_state(
                        runtime_state["session_state"],
                        resolved_mode=resolved_mode,
                        current_stage="router_replanned",
                    )
                    graph = self.compile_taskgraph(
                        course_name=course_name,
                        mode_hint=mode_hint,
                        user_message=user_message,
                        history=history_list,
                        plan=plan,
                        session_state=runtime_state["session_state"],
                        stream=False,
                    )
                    runtime_state["graph"] = graph
                    self._install_runtime_controls(runtime_state, graph)
                    self._mark_step(graph, "plan_intent", "completed")
                    self._refresh_session_state_from_graph(runtime_state, graph)
                    add_event(
                        "taskgraph_recompiled",
                        graph_id=graph.graph_id,
                        session_id=graph.session_id,
                        resolved_mode=resolved_mode,
                        route=graph.route,
                        step_count=len(graph.steps),
                        digest=str(graph.metadata.get("digest", "")),
                        stream=False,
                    )
                    agent_context = self._build_agent_context(
                        course_name=course_name,
                        resolved_mode=resolved_mode,
                        user_message=user_message,
                        history=history_list,
                        plan=plan,
                        runtime_state=runtime_state,
                    )
                    runtime_state["agent_context"] = agent_context
                    self._mark_step(graph, "build_agent_context", "completed")
                    if self._step(graph, "detect_submission") is not None:
                        self._mark_step(graph, "detect_submission", "completed", **graph.metadata.get("route_meta", {}))
                    self._refresh_session_state_from_graph(runtime_state, graph)
                    response = self._dispatch_sync(
                        course_name=course_name,
                        user_message=user_message,
                        plan=plan,
                        resolved_mode=resolved_mode,
                        runtime_state=runtime_state,
                        history=history_list,
                    )
                    notice = self._persist_runtime_effects(course_name=course_name, runtime_state=runtime_state)
                    if notice:
                        response = response.model_copy(update={"content": f"{response.content}{notice}"})
                    self._mark_step(graph, graph.route, "completed")
                    self._mark_step(graph, "persist_session_state", "completed")
                    if graph.route == "run_grade":
                        self._mark_step(graph, "persist_records", "completed")
                        self._mark_step(graph, "persist_memory", "completed")
                    self._mark_step(graph, "synthesize_final", "completed")
                    self._refresh_session_state_from_graph(runtime_state, graph)
                    self.runner._persist_session_state(runtime_state["session_state"])
                    response = response.model_copy(
                        update={
                            "tool_calls": self.runner._final_internal_tool_calls(
                                session_state=runtime_state["session_state"],
                                history_summary_state=dict(runtime_state["session_state"].history_summary_state or {}),
                                extra_tool_calls=self._carry_visible_internal_tool_calls(response.tool_calls)
                                + list((runtime_state.get("_runtime_effects", {}) or {}).get("final_tool_calls", []) or []),
                            )
                        }
                    )
                    self._reload_session_state(course_name, runtime_state)
                    self._clear_runtime_controls(runtime_state)

        return response, plan, graph

    def execute_stream(
        self,
        *,
        course_name: str,
        mode_hint: str,
        user_message: str,
        state: Dict[str, Any] | None,
        history: List[Dict[str, str]] | None,
    ) -> Iterator[Any]:
        plan, runtime_state, session_state, resolved_mode, history_list = self._prepare_request(
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            state=state,
            history=history,
        )

        graph = self.compile_taskgraph(
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            history=history_list,
            plan=plan,
            session_state=session_state,
            stream=True,
        )
        runtime_state["graph"] = graph
        self._install_runtime_controls(runtime_state, graph)
        self._mark_step(graph, "plan_intent", "completed")
        runtime_state["session_state"] = self.runner._update_session_state(
            runtime_state["session_state"],
            current_stage="runtime_compiled",
        )
        self._refresh_session_state_from_graph(runtime_state, graph)
        add_event(
            "taskgraph_compiled",
            graph_id=graph.graph_id,
            session_id=graph.session_id,
            resolved_mode=resolved_mode,
            route=graph.route,
            step_count=len(graph.steps),
            digest=str(graph.metadata.get("digest", "")),
            stream=True,
        )

        enable_replan = os.getenv("ENABLE_ROUTER_REPLAN", "1") == "1"
        if enable_replan and plan.need_rag and self.runner.load_retriever(course_name) is None:
            reason = "检索为空（索引缺失或未构建）"
            new_plan = self.runner.router.replan(
                user_message=user_message,
                mode=mode_hint,
                course_name=course_name,
                previous_plan=plan,
                reason=reason,
            )
            if new_plan.model_dump() != plan.model_dump():
                self.runner.logger.info("[replan] stream_precheck mode=%s reason=%s%s", resolved_mode, reason, self.runner._trace_tag())
                plan = new_plan
                resolved_mode = str(getattr(plan, "resolved_mode", "") or mode_hint)
                runtime_state["session_state"] = self.runner._update_session_state(
                    runtime_state["session_state"],
                    resolved_mode=resolved_mode,
                    current_stage="router_replanned",
                )
                graph = self.compile_taskgraph(
                    course_name=course_name,
                    mode_hint=mode_hint,
                    user_message=user_message,
                    history=history_list,
                    plan=plan,
                    session_state=runtime_state["session_state"],
                    stream=True,
                )
                runtime_state["graph"] = graph
                self._install_runtime_controls(runtime_state, graph)
                self._mark_step(graph, "plan_intent", "completed")
                self._refresh_session_state_from_graph(runtime_state, graph)
                add_event(
                    "taskgraph_recompiled",
                    graph_id=graph.graph_id,
                    session_id=graph.session_id,
                    resolved_mode=resolved_mode,
                    route=graph.route,
                    step_count=len(graph.steps),
                    digest=str(graph.metadata.get("digest", "")),
                    stream=True,
                )

        try:
            if self._step(graph, "prefetch_rag") is not None:
                self._mark_step(graph, "prefetch_rag", "completed")
            if self._step(graph, "prefetch_memory") is not None:
                self._mark_step(graph, "prefetch_memory", "completed")
            agent_context = self._build_agent_context(
                course_name=course_name,
                resolved_mode=resolved_mode,
                user_message=user_message,
                history=history_list,
                plan=plan,
                runtime_state=runtime_state,
            )
            runtime_state["agent_context"] = agent_context
            self._mark_step(graph, "build_agent_context", "completed")
            if self._step(graph, "detect_submission") is not None:
                self._mark_step(graph, "detect_submission", "completed", **graph.metadata.get("route_meta", {}))
            self._refresh_session_state_from_graph(runtime_state, graph)
            yield from self._dispatch_stream(
                course_name=course_name,
                user_message=user_message,
                plan=plan,
                resolved_mode=resolved_mode,
                runtime_state=runtime_state,
                history=history_list,
            )
            notice = self._persist_runtime_effects(course_name=course_name, runtime_state=runtime_state)
            if notice:
                yield notice
        except Exception as exc:
            if self._strict_runtime():
                raise
            session_state = runtime_state["session_state"]
            fallback_event = {
                "reason": f"runtime_step_failed:{type(exc).__name__}",
                "from_path": "new_runtime",
                "to_path": "compat_mode",
                "route": graph.route,
            }
            self.runner.telemetry_service.record_fallback(
                session_id=session_state.session_id,
                request_id=str(session_state.metadata.get("request_id", "")),
                agent="runtime",
                step_name="dispatch_stream",
                reason=fallback_event["reason"],
                from_path="new_runtime",
                to_path="compat_mode",
            )
            runtime_state["session_state"] = self.runner._update_session_state(
                session_state,
                fallback_flags=list(session_state.fallback_flags) + [fallback_event["reason"]],
                metadata={
                    **session_state.metadata,
                    "fallback_events": list(session_state.metadata.get("fallback_events", [])) + [fallback_event],
                },
            )
            runtime_state.pop("agent_context", None)
            self._clear_runtime_controls(runtime_state)
            self._refresh_session_state_from_graph(runtime_state, graph)
            yield from self._dispatch_stream(
                course_name=course_name,
                user_message=user_message,
                plan=plan,
                resolved_mode=resolved_mode,
                runtime_state=runtime_state,
                history=history_list,
            )
            self._reload_session_state(course_name, runtime_state)
        self._mark_step(graph, graph.route, "completed")
        self._mark_step(graph, "persist_session_state", "completed")
        if graph.route == "run_grade":
            self._mark_step(graph, "persist_records", "completed")
            self._mark_step(graph, "persist_memory", "completed")
        self._mark_step(graph, "synthesize_final", "completed")
        self._refresh_session_state_from_graph(runtime_state, graph)
        self.runner._persist_session_state(runtime_state["session_state"])
        yield self.runner.event_bus.tool_calls(
            self.runner._final_internal_tool_calls(
                session_state=runtime_state["session_state"],
                history_summary_state=dict(runtime_state["session_state"].history_summary_state or {}),
                extra_tool_calls=list((runtime_state.get("_runtime_effects", {}) or {}).get("final_tool_calls", []) or []),
            )
        )
        self._reload_session_state(course_name, runtime_state)
        self._clear_runtime_controls(runtime_state)
