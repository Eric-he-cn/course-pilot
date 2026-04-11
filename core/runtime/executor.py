"""ExecutionRuntime: compile and execute TaskGraph against the existing runner."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Tuple

from core.metrics import add_event
from core.runtime.taskgraph import TaskGraphStepV1, TaskGraphV1

if TYPE_CHECKING:
    from backend.schemas import ChatMessage, PlanPlusV1, SessionStateV1
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
        session_state = self.runner._update_session_state(
            session_state,
            requested_mode_hint=mode_hint,
            resolved_mode=resolved_mode,
            task_full_text=user_message,
            task_summary=self.runner._summarize_task_text(user_message),
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
        runtime_state["session_state"] = self.runner._update_session_state(
            runtime_state["session_state"],
            last_taskgraph_digest=str(graph.metadata.get("digest", "")),
            current_stage="runtime_compiled",
            metadata={
                **runtime_state["session_state"].metadata,
                "taskgraph_route": graph.route,
                "taskgraph_steps": graph.step_names(),
            },
        )
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

        response = self._dispatch_sync(
            course_name=course_name,
            user_message=user_message,
            plan=plan,
            resolved_mode=resolved_mode,
            runtime_state=runtime_state,
            history=history_list,
        )

        enable_replan = os.getenv("ENABLE_ROUTER_REPLAN", "1") == "1"
        if enable_replan:
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
                    runtime_state["session_state"] = self.runner._update_session_state(
                        runtime_state["session_state"],
                        last_taskgraph_digest=str(graph.metadata.get("digest", "")),
                    )
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
                    response = self._dispatch_sync(
                        course_name=course_name,
                        user_message=user_message,
                        plan=plan,
                        resolved_mode=resolved_mode,
                        runtime_state=runtime_state,
                        history=history_list,
                    )

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
        runtime_state["session_state"] = self.runner._update_session_state(
            runtime_state["session_state"],
            last_taskgraph_digest=str(graph.metadata.get("digest", "")),
            current_stage="runtime_compiled",
            metadata={
                **runtime_state["session_state"].metadata,
                "taskgraph_route": graph.route,
                "taskgraph_steps": graph.step_names(),
            },
        )
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
                runtime_state["session_state"] = self.runner._update_session_state(
                    runtime_state["session_state"],
                    last_taskgraph_digest=str(graph.metadata.get("digest", "")),
                )
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

        yield from self._dispatch_stream(
            course_name=course_name,
            user_message=user_message,
            plan=plan,
            resolved_mode=resolved_mode,
            runtime_state=runtime_state,
            history=history_list,
        )
