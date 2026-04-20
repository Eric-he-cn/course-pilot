"""ExecutionRuntime: compile and execute TaskGraph against the existing runner."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Tuple

from core.metrics import add_event
from core.runtime.taskgraph import TaskGraphStepV1, TaskGraphV1

if TYPE_CHECKING:
    from backend.schemas import AgentContextV1, ChatMessage, PlanPlusV1, PrefetchBundleV1, SessionStateV1
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
            latest_submission={
                "artifact_kind": "practice"
                if str(getattr(plan, "workflow_template", "") or "").startswith("practice_then")
                else ("exam" if str(getattr(plan, "workflow_template", "") or "").startswith("exam_then") else ""),
                "source_message": user_message,
                "session_id": session_state.session_id,
            }
            if str(getattr(plan, "action_kind", "") or "") in {"practice_grade", "exam_grade"}
            else session_state.latest_submission,
            metadata={
                **dict(session_state.metadata or {}),
                "workflow_template": str(getattr(plan, "workflow_template", "") or ""),
                "action_kind": str(getattr(plan, "action_kind", "") or ""),
                "tool_policy_profile": str(getattr(plan, "tool_policy_profile", "") or ""),
                "context_budget_profile": str(getattr(plan, "context_budget_profile", "") or ""),
                "tool_budget": dict(getattr(plan, "tool_budget", {}) or {}),
                "allowed_tool_groups": list(getattr(plan, "allowed_tool_groups", []) or []),
                "route_reason": str(getattr(plan, "route_reason", "") or ""),
            },
        )
        runtime_state["session_id"] = session_state.session_id
        runtime_state["session_state"] = session_state
        return plan, runtime_state, session_state, resolved_mode, history_list

    @staticmethod
    def _workflow_templates() -> set[str]:
        return {
            "learn_only",
            "practice_only",
            "exam_only",
            "learn_then_practice",
            "practice_then_review",
            "exam_then_review",
        }

    def compile_template(
        self,
        *,
        plan: "PlanPlusV1",
        resolved_mode: str,
        user_message: str,
    ) -> Tuple[str, str, Dict[str, Any]]:
        metadata: Dict[str, Any] = {}
        raw_template = str(getattr(plan, "workflow_template", "") or "").strip().lower()
        template = raw_template
        default_like = raw_template == "learn_only" and resolved_mode != "learn"
        if template not in self._workflow_templates() or default_like:
            template = {
                "learn": "learn_only",
                "practice": "practice_only",
                "exam": "exam_only",
            }.get(resolved_mode, "learn_only")

        action_kind = str(getattr(plan, "action_kind", "") or "").strip() or {
            "learn_only": "learn_explain",
            "practice_only": "practice_generate",
            "exam_only": "exam_generate",
            "learn_then_practice": "learn_then_practice",
            "practice_then_review": "practice_grade",
            "exam_then_review": "exam_grade",
        }.get(template, "learn_explain")

        if template in {"practice_only", "learn_then_practice"}:
            _, _, num_questions, question_type = self.runner._resolve_quiz_request(user_message)
            metadata["num_questions"] = num_questions
            metadata["question_type"] = question_type
            metadata["generator_route"] = "run_exam" if num_questions > 1 else "run_quiz"
        if template == "learn_only":
            return template, action_kind, metadata
        if template == "practice_only":
            return template, action_kind, metadata
        if template == "exam_only":
            metadata["generator_route"] = "run_exam"
            return template, action_kind, metadata
        if template == "learn_then_practice":
            metadata.setdefault("generator_route", "run_quiz")
            return template, action_kind, metadata
        metadata["artifact_kind"] = "practice" if template == "practice_then_review" else "exam"
        return template, action_kind, metadata

    def validate_template_preconditions(
        self,
        *,
        template: str,
        session_state: "SessionStateV1",
    ) -> List[str]:
        issues: List[str] = []
        if template == "practice_then_review" and not (session_state.active_practice or session_state.last_quiz or session_state.last_exam):
            issues.append("missing_active_practice")
        if template == "exam_then_review" and not (session_state.active_exam or session_state.last_exam):
            issues.append("missing_active_exam")
        return issues

    def compile_steps_from_template(
        self,
        *,
        template: str,
        action_kind: str,
        route_meta: Dict[str, Any],
        stream: bool,
        plan: "PlanPlusV1",
        resolved_mode: str,
        user_message: str,
    ) -> Tuple[str, List[TaskGraphStepV1], List[Dict[str, Any]], bool]:
        persist_memory_needed = action_kind in {"practice_grade", "exam_grade"} or (
            resolved_mode == "learn" and self.runner._should_persist_learn_episode(user_message)
        )
        execute_plan: List[Dict[str, Any]] = []
        if template == "learn_only":
            execute_plan = [{"step_name": "run_tutor", "owner_mode": "learn"}]
        elif template == "practice_only":
            execute_plan = [{"step_name": str(route_meta.get("generator_route", "run_quiz")), "owner_mode": "practice"}]
        elif template == "exam_only":
            execute_plan = [{"step_name": "run_exam", "owner_mode": "exam"}]
        elif template == "learn_then_practice":
            execute_plan = [
                {"step_name": "run_tutor", "owner_mode": "learn"},
                {"step_name": str(route_meta.get("generator_route", "run_quiz")), "owner_mode": "practice"},
            ]
        elif template == "practice_then_review":
            execute_plan = [{"step_name": "run_grade", "owner_mode": "practice"}]
        else:
            execute_plan = [{"step_name": "run_grade", "owner_mode": "exam"}]

        steps: List[TaskGraphStepV1] = [TaskGraphStepV1(step_name="plan_intent", phase="plan", stream=stream)]
        if plan.need_rag:
            steps.append(TaskGraphStepV1(step_name="prefetch_rag", phase="prefetch", stream=stream))
        if getattr(plan, "need_memory", True):
            steps.append(TaskGraphStepV1(step_name="prefetch_memory", phase="prefetch", stream=stream))
        steps.append(TaskGraphStepV1(step_name="build_agent_context", phase="context", stream=stream))
        if template in {"practice_then_review", "exam_then_review"}:
            steps.append(
                TaskGraphStepV1(
                    step_name="detect_submission",
                    phase="route",
                    stream=stream,
                    metadata={"resolved_mode": resolved_mode, **route_meta},
                )
            )
        for execute in execute_plan:
            steps.append(
                TaskGraphStepV1(
                    step_name=execute["step_name"],  # type: ignore[arg-type]
                    phase="execute",
                    stream=stream,
                    side_effect=execute["step_name"] == "run_grade",
                    metadata={
                        "owner_mode": execute["owner_mode"],
                        "workflow_template": template,
                        "tool_policy_profile": str(getattr(plan, "tool_policy_profile", "") or ""),
                        "context_budget_profile": str(getattr(plan, "context_budget_profile", "") or ""),
                    },
                )
            )
        steps.append(TaskGraphStepV1(step_name="persist_session_state", phase="persist", side_effect=True, stream=stream))
        if action_kind in {"practice_grade", "exam_grade"}:
            steps.append(TaskGraphStepV1(step_name="persist_records", phase="persist", side_effect=True, stream=stream))
        if persist_memory_needed:
            steps.append(TaskGraphStepV1(step_name="persist_memory", phase="persist", side_effect=True, stream=stream))
        steps.append(TaskGraphStepV1(step_name="synthesize_final", phase="finalize", stream=stream))
        primary_route = str(execute_plan[-1]["step_name"])
        return primary_route, steps, execute_plan, persist_memory_needed

    @staticmethod
    def _graph_digest(graph: TaskGraphV1) -> str:
        payload = {
            "session_id": graph.session_id,
            "mode_hint": graph.mode_hint,
            "resolved_mode": graph.resolved_mode,
            "workflow_template": graph.workflow_template,
            "action_kind": graph.action_kind,
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

    def _build_prefetch_bundle(
        self,
        *,
        course_name: str,
        resolved_mode: str,
        user_message: str,
        history: List[Dict[str, str]],
        plan: "PlanPlusV1",
        runtime_state: Dict[str, Any],
    ) -> tuple["PrefetchBundleV1", Dict[str, Any]]:
        from backend.schemas import PrefetchBundleV1

        retrieval_query = self.runner._plan_retrieval_query(plan, user_message)
        memory_query = self.runner._plan_memory_query(plan, user_message)
        prefetch_signature = json.dumps(
            {
                "course_name": course_name,
                "resolved_mode": resolved_mode,
                "question_raw": str(getattr(plan, "question_raw", "") or user_message),
                "retrieval_query": retrieval_query,
                "memory_query": memory_query,
                "need_rag": bool(getattr(plan, "need_rag", True)),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        cached_bundle = runtime_state.get("prefetch_bundle")
        if isinstance(cached_bundle, PrefetchBundleV1):
            cached_signature = str((cached_bundle.metadata or {}).get("prefetch_signature", "") or "")
            if cached_signature and cached_signature == prefetch_signature:
                add_event(
                    "prefetch_bundle_cache_hit",
                    course_name=course_name,
                    resolved_mode=resolved_mode,
                )
                sections = {
                    "history_context": cached_bundle.candidate_history_context,
                    "rag_context": cached_bundle.candidate_rag_context,
                    "memory_context": cached_bundle.candidate_memory_context,
                    "context": cached_bundle.candidate_merged_context,
                }
                return cached_bundle, sections

        session_state = runtime_state["session_state"]
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
        if str(getattr(plan, "action_kind", "") or "") in {"practice_grade", "exam_grade"}:
            mem_agent = "grader"
            mem_phase = "grade"
        elif resolved_mode in {"practice", "exam"}:
            mem_agent = "quizzer"
            mem_phase = "generate"

        with ThreadPoolExecutor(max_workers=2) as pool:
            rag_ctx = copy_context()
            mem_ctx = copy_context()
            rag_future = pool.submit(
                rag_ctx.run,
                self.runner.rag_service.retrieve,
                course_name=course_name,
                question_raw=str(getattr(plan, "question_raw", "") or user_message),
                retrieval_query=retrieval_query,
                mode=resolved_mode,
                need_rag=bool(getattr(plan, "need_rag", True)),
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于已有上下文继续）",
            )
            mem_future = pool.submit(
                mem_ctx.run,
                self.runner.memory_service.prefetch_history_ctx,
                query=memory_query,
                course_name=course_name,
                mode=resolved_mode,
                agent=mem_agent,
                phase=mem_phase,
            )
            rag_text, citations, retrieval_empty = rag_future.result()
            memory_ctx = mem_future.result()

        packed = self.runner.context_budgeter.build_context(
            query=retrieval_query,
            history=history,
            rag_text=rag_text,
            memory_text=memory_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
            mode=resolved_mode,
            history_summary_state=history_summary_state,
            pending_history=pending_history,
            recent_history=recent_history,
            history_state_metrics=history_metrics,
        )
        self.runner._log_context_budget(resolved_mode, packed)
        sections = self.runner._context_sections_from_packed(packed)
        bundle = PrefetchBundleV1(
            session_snapshot=runtime_state["session_state"],
            candidate_history_context=sections["history_context"],
            candidate_rag_context=sections["rag_context"],
            candidate_memory_context=sections["memory_context"],
            candidate_merged_context=sections["context"],
            citations=list(citations or []),
            constraints={
                "mode": resolved_mode,
                "workflow_template": str(getattr(plan, "workflow_template", "") or ""),
                "action_kind": str(getattr(plan, "action_kind", "") or ""),
            },
            tool_scope={
                "permission_mode": runtime_state["session_state"].permission_mode,
                "allowed_tools": list(getattr(plan, "allowed_tools", []) or []),
                "tool_budget": dict(getattr(plan, "tool_budget", {}) or {}),
                "allowed_tool_groups": list(getattr(plan, "allowed_tool_groups", []) or []),
                "tool_budget_snapshot": {
                    "limits": {
                        "per_request_total": dict(getattr(plan, "tool_budget", {}) or {}).get("per_request_total"),
                        "per_round": dict(getattr(plan, "tool_budget", {}) or {}).get("per_round"),
                        "per_tool": dict(getattr(plan, "tool_budget", {}) or {}).get("per_tool", {}),
                    },
                    "usage": {"executed_total": 0, "current_round": 1, "current_round_used": 0, "per_tool_used": {}},
                },
            },
            metadata={
                "retrieval_query": retrieval_query,
                "memory_query": memory_query,
                "retrieval_empty": retrieval_empty,
                "context_budget": self.runner._context_budget_payload(resolved_mode, len(history), packed),
                "prefetch_signature": prefetch_signature,
            },
        )
        return bundle, sections

    def _agent_for_step(self, step_name: str):
        if step_name == "run_tutor":
            return self.runner.tutor
        if step_name in {"run_quiz", "run_exam"}:
            return self.runner.quizmaster
        return self.runner.grader

    def _build_agent_context_for_step(
        self,
        *,
        step_name: str,
        plan: "PlanPlusV1",
        runtime_state: Dict[str, Any],
        prefetch_bundle: "PrefetchBundleV1",
    ) -> "AgentContextV1":
        agent = self._agent_for_step(step_name)
        session_state = runtime_state["session_state"]
        agent_context = agent.build_context(
            session_state,
            history_context=prefetch_bundle.candidate_history_context,
            rag_context=prefetch_bundle.candidate_rag_context,
            memory_context=prefetch_bundle.candidate_memory_context,
            merged_context=prefetch_bundle.candidate_merged_context,
            context=prefetch_bundle.candidate_merged_context,
            citations=list(prefetch_bundle.citations or []),
            constraints={
                **dict(prefetch_bundle.constraints or {}),
                "step_name": step_name,
            },
            allowed_tools=list(getattr(plan, "allowed_tools", []) or []),
            tool_scope=dict(prefetch_bundle.tool_scope or {}),
            metadata=dict(prefetch_bundle.metadata or {}),
        )
        agent_context.metadata["retrieval_empty"] = bool(prefetch_bundle.metadata.get("retrieval_empty", False))
        agent_context.metadata["context_budget"] = dict(prefetch_bundle.metadata.get("context_budget", {}) or {})
        return agent_context

    def _build_agent_context(
        self,
        *,
        course_name: str,
        resolved_mode: str,
        user_message: str,
        history: List[Dict[str, str]],
        plan: "PlanPlusV1",
        runtime_state: Dict[str, Any],
        step_name: str | None = None,
    ) -> "AgentContextV1":
        """兼容旧测试/探针调用：先预取，再交给对应 Agent 组装上下文。"""

        prefetch_bundle, _ = self._build_prefetch_bundle(
            course_name=course_name,
            resolved_mode=resolved_mode,
            user_message=user_message,
            history=history,
            plan=plan,
            runtime_state=runtime_state,
        )
        actual_step = step_name or {
            "learn": "run_tutor",
            "practice": "run_quiz",
            "exam": "run_exam",
        }.get(resolved_mode, "run_tutor")
        return self._build_agent_context_for_step(
            step_name=actual_step,
            plan=plan,
            runtime_state=runtime_state,
            prefetch_bundle=prefetch_bundle,
        )

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
        if resolved_mode == "learn":
            return self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)
        if resolved_mode == "practice":
            return self.runner.run_practice_mode(course_name, user_message, plan, runtime_state, history)
        if resolved_mode == "exam":
            return self.runner.run_exam_mode(course_name, user_message, plan, history, state=runtime_state)
        return self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)

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
        workflow_template, action_kind, route_meta = self.compile_template(
            plan=plan,
            resolved_mode=resolved_mode,
            user_message=user_message,
        )
        route, steps, execute_plan, _persist_memory_needed = self.compile_steps_from_template(
            template=workflow_template,
            action_kind=action_kind,
            route_meta=route_meta,
            stream=stream,
            plan=plan,
            resolved_mode=resolved_mode,
            user_message=user_message,
        )

        graph = TaskGraphV1(
            graph_id=uuid.uuid4().hex,
            session_id=session_state.session_id,
            mode_hint=mode_hint,  # type: ignore[arg-type]
            resolved_mode=resolved_mode,  # type: ignore[arg-type]
            workflow_template=workflow_template,  # type: ignore[arg-type]
            action_kind=action_kind,  # type: ignore[arg-type]
            route=route,  # type: ignore[arg-type]
            steps=steps,
            metadata={
                "course_name": course_name,
                "stream": stream,
                "route_meta": route_meta,
                "execute_plan": execute_plan,
            },
        )
        digest = self._graph_digest(graph)
        graph.metadata["digest"] = digest
        return graph

    def _execute_plan(self, graph: TaskGraphV1) -> List[Dict[str, Any]]:
        raw = graph.metadata.get("execute_plan", [])
        if not isinstance(raw, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            step_name = str(item.get("step_name", "") or "").strip()
            owner_mode = str(item.get("owner_mode", "") or "").strip()
            if step_name and owner_mode:
                out.append({"step_name": step_name, "owner_mode": owner_mode})
        return out

    def _set_runtime_execute_step(self, runtime_state: Dict[str, Any], step_name: str) -> None:
        runtime_state["_runtime_route"] = step_name
        runtime_state["_runtime_step_name"] = step_name

    def _maybe_replan_for_template(
        self,
        *,
        course_name: str,
        mode_hint: str,
        user_message: str,
        history: List[Dict[str, str]],
        runtime_state: Dict[str, Any],
        plan: "PlanPlusV1",
    ) -> tuple["PlanPlusV1", str]:
        issues = self.validate_template_preconditions(
            template=str(getattr(plan, "workflow_template", "") or ""),
            session_state=runtime_state["session_state"],
        )
        enable_replan = os.getenv("ENABLE_ROUTER_REPLAN", "1") == "1"
        if not issues or not enable_replan:
            return plan, str(getattr(plan, "resolved_mode", "") or mode_hint)
        reason = "；".join(issues)
        replanned = self.runner.router.replan(
            user_message=user_message,
            mode=mode_hint,
            course_name=course_name,
            previous_plan=plan,
            reason=reason,
        )
        if replanned.model_dump() == plan.model_dump():
            return plan, str(getattr(plan, "resolved_mode", "") or mode_hint)
        resolved_mode = str(getattr(replanned, "resolved_mode", "") or mode_hint)
        runtime_state["session_state"] = self.runner._update_session_state(
            runtime_state["session_state"],
            resolved_mode=resolved_mode,
            current_stage="router_replanned",
        )
        return replanned, resolved_mode

    def _run_owner_sync(
        self,
        *,
        step_name: str,
        owner_mode: str,
        course_name: str,
        user_message: str,
        plan: "PlanPlusV1",
        runtime_state: Dict[str, Any],
        history: List[Dict[str, str]],
    ) -> "ChatMessage":
        self._set_runtime_execute_step(runtime_state, step_name)
        if owner_mode == "learn":
            return self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)
        if owner_mode == "practice":
            return self.runner.run_practice_mode(course_name, user_message, plan, runtime_state, history)
        if owner_mode == "exam":
            return self.runner.run_exam_mode(course_name, user_message, plan, history, state=runtime_state)
        return self.runner.run_learn_mode(course_name, user_message, plan, history, state=runtime_state)

    def _run_owner_stream(
        self,
        *,
        step_name: str,
        owner_mode: str,
        course_name: str,
        user_message: str,
        plan: "PlanPlusV1",
        runtime_state: Dict[str, Any],
        history: List[Dict[str, str]],
    ) -> Iterator[Any]:
        self._set_runtime_execute_step(runtime_state, step_name)
        if owner_mode == "learn":
            yield from self.runner.run_learn_mode_stream(course_name, user_message, plan, history, state=runtime_state)
            return
        if owner_mode == "practice":
            yield from self.runner.run_practice_mode_stream(course_name, user_message, plan, history, state=runtime_state)
            return
        if owner_mode == "exam":
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
        plan, resolved_mode = self._maybe_replan_for_template(
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            history=history_list,
            runtime_state=runtime_state,
            plan=plan,
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
            prefetch_bundle, _prefetch_sections = self._build_prefetch_bundle(
                course_name=course_name,
                resolved_mode=resolved_mode,
                user_message=user_message,
                history=history_list,
                plan=plan,
                runtime_state=runtime_state,
            )
            runtime_state["prefetch_bundle"] = prefetch_bundle
            self._mark_step(graph, "build_agent_context", "completed")
            if self._step(graph, "detect_submission") is not None:
                self._mark_step(graph, "detect_submission", "completed", **graph.metadata.get("route_meta", {}))
            self._refresh_session_state_from_graph(runtime_state, graph)
            responses: List["ChatMessage"] = []
            execute_plan = self._execute_plan(graph)
            for idx, execute in enumerate(execute_plan):
                step_name = str(execute.get("step_name", "") or "")
                owner_mode = str(execute.get("owner_mode", "") or "")
                agent_context = self._build_agent_context_for_step(
                    step_name=step_name,
                    plan=plan,
                    runtime_state=runtime_state,
                    prefetch_bundle=prefetch_bundle,
                )
                runtime_state["agent_context"] = agent_context
                current_question = user_message
                if idx > 0 and step_name in {"run_quiz", "run_exam"}:
                    current_question = str(runtime_state["session_state"].task_summary or user_message)
                response = self._run_owner_sync(
                    step_name=step_name,
                    owner_mode=owner_mode,
                    course_name=course_name,
                    user_message=current_question,
                    plan=plan,
                    runtime_state=runtime_state,
                    history=history_list,
                )
                responses.append(response)
            if len(responses) == 1:
                response = responses[0]
            else:
                combined_content = f"{responses[0].content}\n\n---\n\n{responses[-1].content}"
                merged_citations: List[Any] = []
                for resp in responses:
                    merged_citations.extend(list(resp.citations or []))
                response = responses[-1].model_copy(
                    update={
                        "content": combined_content,
                        "citations": merged_citations or None,
                    }
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
        for execute in self._execute_plan(graph):
            self._mark_step(graph, str(execute.get("step_name", "")), "completed")
        self._mark_step(graph, "persist_session_state", "completed")
        if self._step(graph, "persist_records") is not None:
            self._mark_step(graph, "persist_records", "completed")
        if self._step(graph, "persist_memory") is not None:
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
        plan, resolved_mode = self._maybe_replan_for_template(
            course_name=course_name,
            mode_hint=mode_hint,
            user_message=user_message,
            history=history_list,
            runtime_state=runtime_state,
            plan=plan,
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
        final_tool_calls_needed = True

        try:
            if self._step(graph, "prefetch_rag") is not None:
                self._mark_step(graph, "prefetch_rag", "completed")
            if self._step(graph, "prefetch_memory") is not None:
                self._mark_step(graph, "prefetch_memory", "completed")
            prefetch_bundle, _prefetch_sections = self._build_prefetch_bundle(
                course_name=course_name,
                resolved_mode=resolved_mode,
                user_message=user_message,
                history=history_list,
                plan=plan,
                runtime_state=runtime_state,
            )
            runtime_state["prefetch_bundle"] = prefetch_bundle
            self._mark_step(graph, "build_agent_context", "completed")
            if self._step(graph, "detect_submission") is not None:
                self._mark_step(graph, "detect_submission", "completed", **graph.metadata.get("route_meta", {}))
            self._refresh_session_state_from_graph(runtime_state, graph)
            execute_plan = self._execute_plan(graph)
            for idx, execute in enumerate(execute_plan):
                step_name = str(execute.get("step_name", "") or "")
                owner_mode = str(execute.get("owner_mode", "") or "")
                agent_context = self._build_agent_context_for_step(
                    step_name=step_name,
                    plan=plan,
                    runtime_state=runtime_state,
                    prefetch_bundle=prefetch_bundle,
                )
                runtime_state["agent_context"] = agent_context
                current_question = user_message
                if idx > 0 and step_name in {"run_quiz", "run_exam"}:
                    current_question = str(runtime_state["session_state"].task_summary or user_message)
                    yield "\n\n---\n\n"
                yield from self._run_owner_stream(
                    step_name=step_name,
                    owner_mode=owner_mode,
                    course_name=course_name,
                    user_message=current_question,
                    plan=plan,
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
            execute_plan = self._execute_plan(graph)
            for idx, execute in enumerate(execute_plan):
                step_name = str(execute.get("step_name", "") or "")
                owner_mode = str(execute.get("owner_mode", "") or "")
                current_question = user_message
                if idx > 0 and step_name in {"run_quiz", "run_exam"}:
                    current_question = str(runtime_state["session_state"].task_summary or user_message)
                    yield "\n\n---\n\n"
                yield from self._run_owner_stream(
                    step_name=step_name,
                    owner_mode=owner_mode,
                    course_name=course_name,
                    user_message=current_question,
                    plan=plan,
                    runtime_state=runtime_state,
                    history=history_list,
                )
            self._reload_session_state(course_name, runtime_state)
            final_tool_calls_needed = False
        for execute in self._execute_plan(graph):
            self._mark_step(graph, str(execute.get("step_name", "")), "completed")
        self._mark_step(graph, "persist_session_state", "completed")
        if self._step(graph, "persist_records") is not None:
            self._mark_step(graph, "persist_records", "completed")
        if self._step(graph, "persist_memory") is not None:
            self._mark_step(graph, "persist_memory", "completed")
        self._mark_step(graph, "synthesize_final", "completed")
        self._refresh_session_state_from_graph(runtime_state, graph)
        self.runner._persist_session_state(runtime_state["session_state"])
        if final_tool_calls_needed:
            yield self.runner.event_bus.tool_calls(
                self.runner._final_internal_tool_calls(
                    session_state=runtime_state["session_state"],
                    history_summary_state=dict(runtime_state["session_state"].history_summary_state or {}),
                    extra_tool_calls=list((runtime_state.get("_runtime_effects", {}) or {}).get("final_tool_calls", []) or []),
                )
            )
        self._reload_session_state(course_name, runtime_state)
        self._clear_runtime_controls(runtime_state)
