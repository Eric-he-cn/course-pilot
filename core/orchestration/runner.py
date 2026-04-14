"""
【模块说明】
- 主要作用：实现系统主编排器，统一调度 Router/Tutor/Grader、RAG、MCP 工具与记忆系统。
- 核心类：OrchestrationRunner。
- 核心流程：run/run_stream（总入口）+ 各模式执行（learn/practice/exam）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import json
import logging
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime
from core.metrics import get_active_trace

from backend.schemas import (
    AgentContextV1,
    ArtifactQuestionV1,
    Plan,
    PlanPlusV1,
    PracticeArtifactV1,
    ExamArtifactV1,
    SessionStateV1,
    ChatMessage,
    RetrievedChunk,
    Quiz,
    GradeReport,
    TutorResult,
    PracticeGradeSignal,
)
from core.agents.router import RouterAgent
from core.agents.tutor import TutorAgent
from core.agents.quizmaster import QuizMasterAgent
from core.agents.grader import GraderAgent
from core.orchestration.context_budgeter import ContextBudgeter
from core.runtime import ExecutionRuntime
from core.services import (
    MemoryService,
    RAGService,
    TelemetryService,
    WorkspaceStore,
    get_default_event_bus,
    get_default_tool_hub,
)
from rag.retrieve import Retriever
from mcp_tools.client import MCPTools
# 说明：练习/考试的出题与评分已拆分到 QuizMaster/Grader，Runner 只负责编排与持久化。


class OrchestrationRunner:
    """课程学习系统主编排器。"""
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR", "./data/workspaces")
        self.data_dir = data_dir
        self.workspace_store = WorkspaceStore(self.data_dir)
        self.rag_service = RAGService(self.workspace_store)
        self.memory_service = MemoryService()
        self.telemetry_service = TelemetryService()
        self.event_bus = get_default_event_bus()
        self.tool_hub = get_default_tool_hub()
        
        # 初始化各 Agent
        agent_services = {
            "memory_service": self.memory_service,
            "telemetry_service": self.telemetry_service,
            "tool_hub": self.tool_hub,
            "event_bus": self.event_bus,
        }
        self.router = RouterAgent(**agent_services)
        self.tutor = TutorAgent(**agent_services)
        self.quizmaster = QuizMasterAgent(**agent_services)
        self.grader = GraderAgent(**agent_services)
        self.tools = MCPTools()
        self.context_budgeter = ContextBudgeter()
        self.runtime = ExecutionRuntime(self)
        self._session_state_store: Dict[str, SessionStateV1] = {}
        # 保存本轮执行元信息，供 Replan 判定使用。
        self._last_run_meta: Dict[str, Any] = {}
        self.logger = logging.getLogger("runner")

    @staticmethod
    def _trace_tag() -> str:
        trace = get_active_trace()
        if trace is None:
            return ""
        request_id = str((trace.meta or {}).get("request_id", "")).strip() or "unknown"
        return f" request_id={request_id} trace_id={trace.trace_id}"

    """执行质量量化：长度、句子数和异常信号综合判断。"""
    @staticmethod
    def _quality_metrics(content: str) -> Dict[str, Any]:
        import re

        text = (content or "").strip()
        min_chars = int(os.getenv("REPLAN_MIN_CHARS", "160"))
        min_sentences = int(os.getenv("REPLAN_MIN_SENTENCES", "2"))
        sentence_count = len(re.findall(r"[。！？!?\.]", text))
        bad_signals = [
            "Error calling LLM",
            "工具调用失败",
            "请重试",
            "（工具调用出错",
            "MCP tools/call failed",
        ]
        has_bad_signal = any(sig in text for sig in bad_signals)
        too_short = len(text) < min_chars
        too_few_sentences = sentence_count < min_sentences
        low_quality = has_bad_signal or too_short or too_few_sentences
        return {
            "low_quality": low_quality,
            "chars": len(text),
            "sentences": sentence_count,
            "has_bad_signal": has_bad_signal,
            "min_chars": min_chars,
            "min_sentences": min_sentences,
        }

    """检测工具链路失败信号（模型回复中显式暴露错误）。"""
    @staticmethod
    def _has_tool_failure_signal(content: str) -> bool:
        text = (content or "")
        signals = [
            "工具调用失败",
            "（工具调用出错",
            "MCP tools/call failed",
            "\"success\": false",
            "Error calling LLM",
        ]
        return any(sig in text for sig in signals)

    """收集本轮是否需要触发 Replan 的原因。"""
    def _collect_replan_reasons(
        self,
        mode: str,
        plan: Plan,
        response: ChatMessage,
    ) -> List[str]:
        reasons: List[str] = []
        meta = self._last_run_meta or {}

        # 存在写文件/写记忆副作用的分支不做 Replan，防止重复写入。
        if meta.get("has_side_effect"):
            return reasons

        if plan.need_rag and meta.get("retrieval_empty"):
            reasons.append("检索为空（索引缺失或未召回到有效片段）")

        if self._has_tool_failure_signal(response.content):
            reasons.append("工具失败（调用失败或工具错误信号）")

        qm = self._quality_metrics(response.content)
        if qm["low_quality"]:
            reasons.append(
                "回答质量偏低"
                f"(chars={qm['chars']}/{qm['min_chars']}, "
                f"sentences={qm['sentences']}/{qm['min_sentences']}, "
                f"bad_signal={int(qm['has_bad_signal'])})"
            )
        return reasons

    """按模式执行一次，作为 Replan 前后复用的统一分发入口。"""
    def _run_mode_once(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        plan: Plan,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None,
    ) -> ChatMessage:
        if mode == "learn":
            return self.run_learn_mode(course_name, user_message, plan, history, state=state)
        if mode == "practice":
            return self.run_practice_mode(course_name, user_message, plan, state, history)
        if mode == "exam":
            return self.run_exam_mode(course_name, user_message, plan, history, state=state)
        return ChatMessage(
            role="assistant",
            content=f"未知模式: {mode}",
            citations=None,
            tool_calls=None,
        )
    
    def get_workspace_path(self, course_name: str) -> str:
        """获取课程工作目录（包含路径穿越防护）。"""
        return self.workspace_store.get_workspace_path(course_name)
    
    def load_retriever(self, course_name: str) -> Optional[Retriever]:
        """按课程加载检索器（未构建索引时返回 None）。"""
        return self.rag_service.load_retriever(course_name)

    @staticmethod
    def _top_k_for_mode(mode: str) -> int:
        m = (mode or "").strip().lower()
        if m == "exam":
            return int(os.getenv("RAG_TOPK_EXAM", "8"))
        if m in {"learn", "practice"}:
            return int(os.getenv("RAG_TOPK_LEARN_PRACTICE", "4"))
        return int(os.getenv("TOP_K_RESULTS", "3"))

    # 兼容测试与旧调用方：当前主链路已改为 rolling summary，但最近历史裁剪口径仍统一为 5 轮。
    @staticmethod
    def _trim_history_recent(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if not history:
            return []
        turns = int(os.getenv("CB_HISTORY_RECENT_TURNS", "5"))
        keep = max(0, turns * 2)
        if keep <= 0:
            return []
        return history[-keep:]

    @staticmethod
    def _plan_question_raw(plan: Plan, user_message: str) -> str:
        text = str(getattr(plan, "question_raw", "") or user_message or "").strip()
        return text or str(user_message or "").strip()

    @staticmethod
    def _plan_retrieval_query(plan: Plan, user_message: str) -> str:
        text = str(getattr(plan, "retrieval_query", "") or "").strip()
        return text or OrchestrationRunner._plan_question_raw(plan, user_message)

    @staticmethod
    def _plan_memory_query(plan: Plan, user_message: str) -> str:
        text = str(getattr(plan, "memory_query", "") or "").strip()
        return text or OrchestrationRunner._plan_retrieval_query(plan, user_message)

    @staticmethod
    def _should_persist_learn_episode(user_message: str) -> bool:
        text = str(user_message or "").strip()
        if not text:
            return False
        explicit_patterns = [
            "记住",
            "帮我记住",
            "请记住",
            "下次提醒",
            "以后提醒",
            "以后按这个偏好",
            "记下来",
            "保存这个偏好",
        ]
        return any(p in text for p in explicit_patterns)

    @staticmethod
    def _empty_history_summary_state() -> Dict[str, Any]:
        recent_raw_turns = max(1, int(os.getenv("CB_HISTORY_RECENT_RAW_TURNS", os.getenv("CB_HISTORY_RECENT_TURNS", "5"))))
        block_turns = max(1, int(os.getenv("CB_HISTORY_SUMMARY_BLOCK_TURNS", "5")))
        max_blocks = max(1, int(os.getenv("CB_HISTORY_SUMMARY_MAX_BLOCKS", "10")))
        return {
            "blocks": [],
            "covered_turns": 0,
            "recent_raw_turns": recent_raw_turns,
            "block_turns": block_turns,
            "max_blocks": max_blocks,
        }

    @classmethod
    def _normalize_history_summary_state(cls, state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        base = cls._empty_history_summary_state()
        if not isinstance(state, dict):
            return base
        blocks = state.get("blocks", [])
        if isinstance(blocks, list):
            base["blocks"] = [dict(b) for b in blocks if isinstance(b, dict)]
        try:
            base["covered_turns"] = max(0, int(state.get("covered_turns", 0) or 0))
        except Exception:
            base["covered_turns"] = 0
        for key in ("recent_raw_turns", "block_turns", "max_blocks"):
            try:
                base[key] = max(1, int(state.get(key, base[key]) or base[key]))
            except Exception:
                pass
        return base

    @staticmethod
    def _history_to_turns(history: List[Dict[str, str]]) -> List[List[Dict[str, str]]]:
        turns: List[List[Dict[str, str]]] = []
        current_user: Optional[Dict[str, str]] = None
        for msg in history or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", "")).strip()
            content = str(msg.get("content", "") or "").strip()
            if role == "user":
                current_user = dict(msg)
            elif role == "assistant":
                if current_user is None:
                    continue
                if not content and not msg.get("tool_calls"):
                    continue
                turns.append([current_user, dict(msg)])
                current_user = None
        return turns

    @staticmethod
    def _flatten_turns(turns: List[List[Dict[str, str]]]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for turn in turns:
            for msg in turn:
                if isinstance(msg, dict):
                    out.append(dict(msg))
        return out

    @classmethod
    def _extract_history_summary_state(cls, history: list) -> Dict[str, Any]:
        payload = cls._extract_internal_meta(history, "history_summary_state")
        return cls._normalize_history_summary_state(payload)

    @staticmethod
    def _build_history_summary_tool_call(state: Dict[str, Any]) -> List[Dict[str, Any]]:
        blocks = list((state or {}).get("blocks", []) or [])
        covered_turns = int((state or {}).get("covered_turns", 0) or 0)
        if not blocks and covered_turns <= 0:
            return []
        return [
            {
                "type": "internal_meta",
                "name": "history_summary_state",
                "payload": state,
            }
        ]

    @staticmethod
    def _merge_internal_tool_calls(*tool_call_groups: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        merged: List[Dict[str, Any]] = []
        for group in tool_call_groups:
            if not isinstance(group, list):
                continue
            for tc in group:
                if isinstance(tc, dict):
                    merged.append(tc)
        return merged or None

    @staticmethod
    def _summarize_task_text(user_message: str, max_chars: int = 160) -> str:
        text = str(user_message or "").strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "..."

    @staticmethod
    def _build_session_state_tool_call(session_state: SessionStateV1) -> List[Dict[str, Any]]:
        return [
            {
                "type": "internal_meta",
                "name": "session_state",
                "payload": session_state.model_dump(),
            }
        ]

    def _final_internal_tool_calls(
        self,
        *,
        session_state: SessionStateV1,
        history_summary_state: Dict[str, Any],
        extra_tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        return self._merge_internal_tool_calls(
            extra_tool_calls,
            self._build_history_summary_tool_call(history_summary_state),
            self._build_session_state_tool_call(session_state),
        )

    def _persist_session_state(self, session_state: SessionStateV1) -> SessionStateV1:
        self._session_state_store[session_state.session_id] = session_state
        self.workspace_store.save_session_state(session_state)
        return session_state

    def _extract_session_state(
        self,
        *,
        history: Optional[List[Dict[str, Any]]],
        course_name: str,
        mode_hint: str,
        user_message: str,
        state: Optional[Dict[str, Any]] = None,
    ) -> SessionStateV1:
        provided_state = (state or {}).get("session_state")
        if isinstance(provided_state, SessionStateV1):
            return provided_state.model_copy(
                update={
                    "course_name": course_name,
                    "requested_mode_hint": mode_hint,
                    "task_full_text": user_message or provided_state.task_full_text,
                    "task_summary": self._summarize_task_text(user_message or provided_state.task_full_text),
                }
            )
        if isinstance(provided_state, dict):
            try:
                restored_state = SessionStateV1.model_validate(provided_state)
                return restored_state.model_copy(
                    update={
                        "course_name": course_name,
                        "requested_mode_hint": mode_hint,
                        "task_full_text": user_message or restored_state.task_full_text,
                        "task_summary": self._summarize_task_text(user_message or restored_state.task_full_text),
                    }
                )
            except Exception:
                pass
        session_id = str((state or {}).get("session_id", "") or "").strip()
        if session_id and session_id in self._session_state_store:
            stored = self._session_state_store[session_id]
            self.telemetry_service.add_event("session_store_lookup", source="memory", hit=True)
            return stored.model_copy(
                update={
                    "course_name": course_name,
                    "requested_mode_hint": mode_hint,
                        "task_full_text": user_message or stored.task_full_text,
                        "task_summary": self._summarize_task_text(user_message or stored.task_full_text),
                    }
                )
        if session_id:
            try:
                stored = self.workspace_store.load_session_state(course_name, session_id)
            except Exception:
                stored = None
            if stored is not None:
                self._session_state_store[session_id] = stored
                self.telemetry_service.add_event("session_store_lookup", source="workspace", hit=True)
                return stored.model_copy(
                    update={
                        "course_name": course_name,
                        "requested_mode_hint": mode_hint,
                        "task_full_text": user_message or stored.task_full_text,
                        "task_summary": self._summarize_task_text(user_message or stored.task_full_text),
                    }
                )
            self.telemetry_service.add_event("session_store_lookup", source="workspace", hit=False)

        payload = self._extract_internal_meta(history or [], "session_state")
        if isinstance(payload, dict):
            try:
                restored = SessionStateV1.model_validate(payload)
                if session_id and restored.session_id != session_id:
                    restored = restored.model_copy(update={"session_id": session_id})
                return restored.model_copy(
                    update={
                        "course_name": course_name,
                        "requested_mode_hint": mode_hint,
                        "task_full_text": user_message or restored.task_full_text,
                        "task_summary": self._summarize_task_text(user_message or restored.task_full_text),
                    }
                )
            except Exception:
                pass

        legacy_history_summary_state = self._extract_history_summary_state(history or [])
        legacy_last_quiz = self._extract_internal_meta(history or [], "quiz_meta")
        legacy_last_exam = self._extract_internal_meta(history or [], "exam_meta")
        active_practice = None
        active_exam = None
        if isinstance(legacy_last_quiz, dict):
            active_practice = {
                "kind": "practice",
                "title": "练习题",
                "instructions": "请回答上述题目，回答完毕后我会为你评分并给出详细讲解。",
                "questions": [
                    {
                        "id": 1,
                        "type": "综合题",
                        "question": str(legacy_last_quiz.get("question", "") or ""),
                        "options": [],
                        "score": 100,
                        "standard_answer": str(legacy_last_quiz.get("standard_answer", "") or ""),
                        "rubric": str(legacy_last_quiz.get("rubric", "") or ""),
                        "chapter": str(legacy_last_quiz.get("chapter", "") or ""),
                        "concept": str(legacy_last_quiz.get("concept", "") or ""),
                        "difficulty": str(legacy_last_quiz.get("difficulty", "") or "medium"),
                    }
                ],
                "total_score": 100,
            }
        if isinstance(legacy_last_exam, dict):
            active_exam = {
                "kind": "exam",
                "title": "模拟考试试卷",
                "instructions": "请将各题答案统一整理后一次性提交。",
                "questions": list(legacy_last_exam.get("answer_sheet", []) or []),
                "total_score": int(legacy_last_exam.get("total_score", 0) or 0),
                "content": "",
            }
        if active_practice is None and mode_hint == "practice":
            inferred_practice = self._extract_quiz_from_history(history or [])
            if inferred_practice and not inferred_practice.startswith("（未能"):
                active_practice = {
                    "kind": "practice",
                    "title": "练习题",
                    "instructions": "请回答上述题目，回答完毕后我会为你评分并给出详细讲解。",
                    "questions": [
                        {
                            "id": 1,
                            "type": "综合题",
                            "question": inferred_practice,
                            "options": [],
                            "score": 100,
                            "standard_answer": "",
                            "rubric": "",
                            "chapter": "",
                            "concept": "",
                            "difficulty": "medium",
                        }
                    ],
                    "total_score": 100,
                    "content": inferred_practice,
                }
        if active_exam is None and mode_hint == "exam":
            inferred_exam = self._extract_exam_from_history(history or [])
            if inferred_exam and not inferred_exam.startswith("（未能"):
                active_exam = {
                    "kind": "exam",
                    "title": "模拟考试试卷",
                    "instructions": "请将各题答案统一整理后一次性提交。",
                    "questions": [],
                    "total_score": 0,
                    "content": inferred_exam,
                }
        return SessionStateV1(
            session_id=session_id or uuid.uuid4().hex,
            course_name=course_name,
            requested_mode_hint=mode_hint,  # type: ignore[arg-type]
            resolved_mode=mode_hint,  # type: ignore[arg-type]
            task_full_text=user_message,
            task_summary=self._summarize_task_text(user_message),
            question_raw=user_message,
            user_intent=user_message,
            retrieval_query=user_message,
            memory_query=user_message,
            current_stage="router_planned",
            current_step_index=0,
            history_summary_state=legacy_history_summary_state,
            last_quiz=legacy_last_quiz,
            last_exam=legacy_last_exam,
            active_practice=active_practice,
            active_exam=active_exam,
        )

    @staticmethod
    def _update_session_state(session_state: SessionStateV1, **updates: Any) -> SessionStateV1:
        return session_state.model_copy(update=updates)

    @staticmethod
    def _coerce_agent_context(value: Any) -> Optional[AgentContextV1]:
        if isinstance(value, AgentContextV1):
            return value
        if isinstance(value, dict):
            try:
                return AgentContextV1.model_validate(value)
            except Exception:
                return None
        return None

    def _init_tool_runtime_context(
        self,
        *,
        course_name: str,
        session_state: SessionStateV1,
        retrieval_query: str,
        memory_query: str,
        question_raw: str,
        permission_mode: str,
        taskgraph_step: str = "",
    ) -> None:
        workspace_path = self.get_workspace_path(course_name)
        notes_dir = os.path.abspath(os.path.join(workspace_path, "notes"))
        trace = get_active_trace()
        trace_meta = dict(getattr(trace, "meta", {}) or {})
        taskgraph_step = str(taskgraph_step or "").strip()
        MCPTools.set_request_context(
            {
            "request_id": str(trace_meta.get("request_id", "") or "").strip(),
            "course_name": course_name,
            "mode": session_state.resolved_mode,
            "user_id": "default",
            "trace_id": str(getattr(trace, "trace_id", "") or "").strip(),
            "budget_state": {},
            "tool_audit": [],
            "idempotency_namespace": f"{session_state.session_id}:{taskgraph_step}" if taskgraph_step else session_state.session_id,
            "notes_dir": notes_dir,
            "memory_query": memory_query,
            "retrieval_query": retrieval_query,
            "question_raw": question_raw,
            "session_id": session_state.session_id,
            "permission_mode": permission_mode,
            "taskgraph_step": taskgraph_step,
            "runtime_route": taskgraph_step,
            "strict_new_runtime": os.getenv("STRICT_NEW_RUNTIME", "0") == "1",
            "tool_budget": dict(session_state.metadata.get("tool_budget", {}) or {}),
            "allowed_tool_groups": list(session_state.metadata.get("allowed_tool_groups", []) or []),
            "workflow_template": str(session_state.metadata.get("workflow_template", "") or ""),
            }
        )

    @staticmethod
    def _extract_tool_audit_refs() -> List[str]:
        audit = MCPTools.get_request_context().tool_audit
        refs: List[str] = []
        for item in audit:
            if not isinstance(item, dict):
                continue
            key = str(item.get("idempotency_key", "") or item.get("signature", "") or item.get("tool_name", "")).strip()
            if key:
                refs.append(key)
        return refs

    @staticmethod
    def _runtime_managed(state: Optional[Dict[str, Any]]) -> bool:
        return bool((state or {}).get("_runtime_managed"))

    @staticmethod
    def _runtime_route_override(state: Optional[Dict[str, Any]]) -> str:
        return str((state or {}).get("_runtime_route", "") or "").strip()

    @staticmethod
    def _runtime_effects(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if state is None:
            return {}
        effects = state.get("_runtime_effects")
        if not isinstance(effects, dict):
            effects = {}
            state["_runtime_effects"] = effects
        return effects

    @staticmethod
    def _runtime_update_state_ref(state: Optional[Dict[str, Any]], session_state: SessionStateV1) -> SessionStateV1:
        if isinstance(state, dict):
            state["session_state"] = session_state
        return session_state

    def _build_agent_context(
        self,
        *,
        session_state: SessionStateV1,
        history: List[Dict[str, Any]],
        retrieval_query: str,
        rag_text: str,
        memory_text: str,
        citations: List[RetrievedChunk],
        mode: str,
        history_summary_state: Optional[Dict[str, Any]] = None,
        pending_history: Optional[List[Dict[str, str]]] = None,
        recent_history: Optional[List[Dict[str, str]]] = None,
        history_metrics: Optional[Dict[str, Any]] = None,
    ) -> tuple[AgentContextV1, Dict[str, Any], Dict[str, str]]:
        packed = self.context_budgeter.build_context(
            query=retrieval_query,
            history=history,
            rag_text=rag_text,
            memory_text=memory_text,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
            mode=mode,
            history_summary_state=history_summary_state,
            pending_history=pending_history,
            recent_history=recent_history,
            history_state_metrics=history_metrics,
        )
        self._log_context_budget(mode, packed)
        sections = self._context_sections_from_packed(packed)
        agent_context = AgentContextV1(
            session_snapshot=session_state,
            history_context=sections["history_context"],
            rag_context=sections["rag_context"],
            memory_context=sections["memory_context"],
            merged_context=sections["context"],
            citations=list(citations or []),
            constraints={"mode": mode},
            tool_scope={
                "permission_mode": session_state.permission_mode,
                "allowed_tools": [],
            },
            metadata={
                "context_budget": self._context_budget_payload(mode, len(history), packed),
            },
        )
        return agent_context, packed, sections

    def _prepare_history_summary_inputs(
        self,
        history: List[Dict[str, str]],
    ) -> tuple[Dict[str, Any], List[Dict[str, str]], List[Dict[str, str]], Dict[str, Any]]:
        state = self._extract_history_summary_state(history)
        turns = self._history_to_turns(history)
        recent_raw_turns = max(1, int(state.get("recent_raw_turns", 5) or 5))
        block_turns = max(1, int(state.get("block_turns", 5) or 5))
        max_blocks = max(1, int(state.get("max_blocks", 10) or 10))

        older_turn_limit = max(0, len(turns) - recent_raw_turns)
        covered_turns = min(max(0, int(state.get("covered_turns", 0) or 0)), older_turn_limit)
        blocks = [dict(b) for b in state.get("blocks", []) if isinstance(b, dict)]
        state_hit = bool(blocks)
        total_block_ms = 0.0

        pending_turns = turns[covered_turns:older_turn_limit]
        while len(pending_turns) >= block_turns:
            batch = pending_turns[:block_turns]
            compressed = self.context_budgeter.compress_history_block(batch)
            turn_start = covered_turns + 1
            covered_turns += block_turns
            turn_end = covered_turns
            blocks.append(
                {
                    "id": f"turns_{turn_start}_{turn_end}",
                    "turn_range": f"{turn_start}-{turn_end}",
                    "summary_text": str(compressed.get("summary_text", "") or "").strip(),
                    "source": str(compressed.get("source", "heuristic") or "heuristic"),
                    "tokens_est": int(compressed.get("tokens_est", 0) or 0),
                    "created_at": datetime.now().isoformat(),
                }
            )
            if len(blocks) > max_blocks:
                blocks = blocks[-max_blocks:]
            total_block_ms += float(compressed.get("elapsed_ms", 0.0) or 0.0)
            pending_turns = pending_turns[block_turns:]

        state = {
            "blocks": blocks,
            "covered_turns": covered_turns,
            "recent_raw_turns": recent_raw_turns,
            "block_turns": block_turns,
            "max_blocks": max_blocks,
        }
        metrics = {
            "history_summary_state_hit": state_hit,
            "history_summary_block_count": len(blocks),
            "history_block_compress_ms": round(total_block_ms, 3) if total_block_ms > 0 else None,
        }
        recent_turns = turns[older_turn_limit:]
        return state, self._flatten_turns(pending_turns), self._flatten_turns(recent_turns), metrics
    
    def run_learn_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None,
        state: Dict[str, Any] = None,
    ) -> ChatMessage:
        """执行学习模式（非流式）。"""
        if history is None:
            history = []
        state = state or {}
        runtime_managed = self._runtime_managed(state)
        retrieval_empty = False
        question_raw = self._plan_question_raw(plan, user_message)
        retrieval_query = self._plan_retrieval_query(plan, user_message)
        memory_query = self._plan_memory_query(plan, user_message)
        agent_context = self._coerce_agent_context(state.get("agent_context"))
        if agent_context is not None:
            session_state = agent_context.session_snapshot
            history_summary_state = dict(session_state.history_summary_state or {})
        else:
            history_summary_state, pending_history, recent_history, history_metrics = self._prepare_history_summary_inputs(history)
            session_state = self._extract_session_state(
                history=history,
                course_name=course_name,
                mode_hint="learn",
                user_message=user_message,
                state=state,
            )
        session_state = self._update_session_state(
            session_state,
            resolved_mode=str(getattr(plan, "resolved_mode", "") or "learn"),
            question_raw=question_raw,
            user_intent=str(getattr(plan, "user_intent", "") or question_raw),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="learn_running",
            current_step_index=1,
            history_summary_state=history_summary_state,
        )
        self._runtime_update_state_ref(state, session_state)
        # 关键步骤：按 plan 决定是否执行 RAG 检索并准备 citations。
        if agent_context is not None:
            citations = list(agent_context.citations or [])
            context_sections = {
                "history_context": agent_context.history_context,
                "rag_context": agent_context.rag_context,
                "memory_context": agent_context.memory_context,
                "context": agent_context.merged_context,
            }
            context = agent_context.merged_context
            retrieval_empty = bool(agent_context.metadata.get("retrieval_empty", False))
        else:
            rag_text, citations, retrieval_empty = self.rag_service.retrieve(
                course_name=course_name,
                question_raw=question_raw,
                retrieval_query=retrieval_query,
                mode="learn",
                need_rag=plan.need_rag,
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于通用知识和已有上下文回答）",
            )
            memory_ctx = self._fetch_history_ctx(
                query=memory_query,
                course_name=course_name,
                mode="learn",
                agent="tutor",
                phase="answer",
            )
            agent_context, packed, context_sections = self._build_agent_context(
                session_state=session_state,
                history=history,
                retrieval_query=retrieval_query,
                rag_text=rag_text,
                memory_text=memory_ctx,
                citations=citations,
                mode="learn",
            )
            agent_context.metadata["retrieval_empty"] = retrieval_empty
            context = agent_context.merged_context

        # 关键步骤：组装 Tutor 入参并触发生成。
        self._init_tool_runtime_context(
            course_name=course_name,
            session_state=session_state,
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            question_raw=question_raw,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            taskgraph_step=self._runtime_route_override(state) or "run_tutor",
        )
        result: TutorResult = self.tutor.teach(
            user_message, course_name, context,
            context_sections=context_sections,
            retrieval_empty=retrieval_empty,
            allowed_tools=plan.allowed_tools,
            history=history,
        )
        # 质量检查：若回答过短或包含错误信号，自动重试一次
        if not self._check_quality(result.content):
            self.logger.info("[quality] retry_once=1 reason=low_quality%s", self._trace_tag())
            result = self.tutor.teach(
                user_message, course_name, context,
                context_sections=context_sections,
                retrieval_empty=retrieval_empty,
                allowed_tools=plan.allowed_tools,
                history=history,
            )

        # 合并 RAG citations 和 Tutor 内部工具调用产生的 citations
        merged_citations = citations + result.citations if citations else result.citations
        if self._should_persist_learn_episode(user_message):
            doc_ids = [c.doc_id for c in merged_citations] if merged_citations else []
            if runtime_managed:
                effects = self._runtime_effects(state)
                effects["persist_memory"] = {
                    "kind": "learn_episode",
                    "course_name": course_name,
                    "question_raw": question_raw,
                    "doc_ids": doc_ids,
                }
            else:
                try:
                    self.memory_service.save_learn_episode(course_name, question_raw, doc_ids)
                except Exception as _mem_err:
                    self.logger.warning("[memory] learn_qa_save_failed err=%s%s", str(_mem_err), self._trace_tag())
        self._last_run_meta = {
            "mode": "learn",
            "retrieval_empty": retrieval_empty,
            "has_side_effect": False,
        }
        session_state = self._update_session_state(
            session_state,
            task_full_text=question_raw,
            task_summary=self._summarize_task_text(question_raw),
            current_stage="learn_completed",
            current_step_index=3,
            selected_memory=context_sections.get("memory_context", ""),
            history_summary_state=history_summary_state,
            tool_audit_refs=self._extract_tool_audit_refs(),
        )
        self._runtime_update_state_ref(state, session_state)
        if not runtime_managed:
            self._persist_session_state(session_state)

        return ChatMessage(
            role="assistant",
            content=result.content,
            citations=merged_citations if merged_citations else None,
            tool_calls=self._final_internal_tool_calls(
                session_state=session_state,
                history_summary_state=history_summary_state,
            ),
        )
    
    def run_practice_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None,
    ) -> ChatMessage:
        """对话式练习模式（非流式）：出题走 QuizMaster，交卷走 Grader。"""
        if history is None:
            history = []
        state = state or {}
        runtime_managed = self._runtime_managed(state)
        route_override = self._runtime_route_override(state)
        retrieval_empty = False
        retrieval_query = self._plan_retrieval_query(plan, user_message)
        memory_query = self._plan_memory_query(plan, user_message)
        agent_context = self._coerce_agent_context(state.get("agent_context"))
        if agent_context is not None:
            session_state = agent_context.session_snapshot
            history_summary_state = dict(session_state.history_summary_state or {})
            citations = list(agent_context.citations or [])
            context_sections = {
                "history_context": agent_context.history_context,
                "rag_context": agent_context.rag_context,
                "memory_context": agent_context.memory_context,
                "context": agent_context.merged_context,
            }
            context = agent_context.merged_context
            retrieval_empty = bool(agent_context.metadata.get("retrieval_empty", False))
        else:
            history_summary_state, pending_history, recent_history, history_metrics = self._prepare_history_summary_inputs(history)
            session_state = self._extract_session_state(
                history=history,
                course_name=course_name,
                mode_hint="practice",
                user_message=user_message,
                state=state,
            )
            rag_text, citations, retrieval_empty = self.rag_service.retrieve(
                course_name=course_name,
                question_raw=str(getattr(plan, "question_raw", "") or user_message),
                retrieval_query=retrieval_query,
                mode="practice",
                need_rag=plan.need_rag,
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于已有上下文出题）",
            )

        answer_submission = route_override == "run_grade" or (
            not route_override and self._is_answer_submission(user_message, history)
        )
        session_state = self._update_session_state(
            session_state,
            resolved_mode=str(getattr(plan, "resolved_mode", "") or "practice"),
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            user_intent=str(getattr(plan, "user_intent", "") or user_message),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="practice_grading" if answer_submission else "practice_generating",
            current_step_index=2 if answer_submission else 1,
            history_summary_state=history_summary_state,
        )
        self._runtime_update_state_ref(state, session_state)
        mem_agent = "grader" if answer_submission else "quizzer"
        mem_phase = "grade" if answer_submission else "generate"
        if agent_context is None:
            history_ctx = self._fetch_history_ctx(
                query=memory_query,
                course_name=course_name,
                mode="practice",
                agent=mem_agent,
                phase=mem_phase,
            )
            agent_context, packed, context_sections = self._build_agent_context(
                session_state=session_state,
                history=history,
                retrieval_query=retrieval_query,
                rag_text=rag_text,
                memory_text=history_ctx,
                citations=citations,
                mode="practice",
                history_summary_state=history_summary_state,
                pending_history=pending_history,
                recent_history=recent_history,
                history_metrics=history_metrics,
            )
            agent_context.metadata["retrieval_empty"] = retrieval_empty
            context = agent_context.merged_context
        history_ctx = context_sections["memory_context"]
        self._init_tool_runtime_context(
            course_name=course_name,
            session_state=session_state,
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            taskgraph_step=route_override or ("run_grade" if answer_submission else "run_quiz"),
        )
        tool_calls = None

        if answer_submission:
            active_practice = session_state.active_practice
            has_exam_meta = bool(active_practice and len(list(active_practice.get("questions", []) or [])) > 1)
            if not has_exam_meta:
                has_exam_meta = bool(self._extract_internal_meta(history, "exam_meta"))
            chunks = []
            if has_exam_meta:
                self.logger.info("[route] practice answer_submission=1 target=grader_exam%s", self._trace_tag())
                exam_paper = self._practice_content_from_artifact(active_practice) or self._extract_exam_from_history(history)
                grade_history_ctx = self._dedupe_grading_history_ctx(history_ctx, exam_paper)
                for chunk in self.grader.grade_exam_stream(
                    exam_paper=exam_paper,
                    student_answer=user_message,
                    course_name=course_name,
                    history_ctx=grade_history_ctx,
                    retrieval_empty=retrieval_empty,
                ):
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            else:
                self.logger.info("[route] practice answer_submission=1 target=grader%s", self._trace_tag())
                quiz_content = self._practice_content_from_artifact(active_practice) or self._extract_quiz_from_history(history)
                grade_history_ctx = self._dedupe_grading_history_ctx(history_ctx, quiz_content)
                for chunk in self.grader.grade_practice_stream(
                    quiz_content=quiz_content,
                    student_answer=user_message,
                    course_name=course_name,
                    history_ctx=grade_history_ctx,
                    retrieval_empty=retrieval_empty,
                ):
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            response_text = "".join(chunks)
            if runtime_managed:
                effects = self._runtime_effects(state)
                effects["persist_records"] = {
                    "kind": "practice_record",
                    "course_name": course_name,
                    "user_message": user_message,
                    "history": list(history),
                    "response_text": response_text,
                }
                effects["persist_memory"] = {
                    "kind": "practice_grade",
                    "course_name": course_name,
                    "user_answer": user_message,
                    "history": list(history),
                    "response_text": response_text,
                }
                effects["record_notice"] = "practice"
            else:
                saved_path = self._save_practice_record(course_name, user_message, history, response_text)
                self._save_grading_to_memory(course_name, user_message, history, response_text)
                response_text += f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"
            session_state = self._update_session_state(
                session_state,
                current_stage="practice_graded",
                current_step_index=3,
                selected_memory=context_sections.get("memory_context", ""),
                history_summary_state=history_summary_state,
                tool_audit_refs=self._extract_tool_audit_refs(),
                latest_submission={
                    "artifact_kind": "practice",
                    "source_message": user_message,
                    "session_id": session_state.session_id,
                },
                latest_grading={
                    "artifact_kind": "practice",
                    "report_text": response_text,
                },
            )
            self._runtime_update_state_ref(state, session_state)
            tool_calls = None
        else:
            self.logger.info("[status] practice generating_quiz%s", self._trace_tag())
            topic, difficulty, num_questions, question_type = self._resolve_quiz_request(user_message)
            if route_override == "run_exam" or num_questions > 1:
                self.logger.info(
                    "[route] practice multi_question=%d use_exam_generator=1%s",
                    num_questions,
                    self._trace_tag(),
                )
                practice_request = f"请生成{num_questions}道{question_type}练习题，主题：{topic}，难度：{difficulty}"
                exam_payload = self.quizmaster.generate_exam_paper(
                    course_name=course_name,
                    user_request=practice_request,
                    context=context,
                    rag_context=context_sections["rag_context"],
                    history_context=context_sections["history_context"],
                    memory_context=context_sections["memory_context"],
                    retrieval_empty=retrieval_empty,
                    prefetched_memory_ctx=context_sections["memory_context"],
                    prefetched_memory_checked=True,
                )
                if not isinstance(exam_payload, dict):
                    exam_payload = {"content": str(exam_payload), "answer_sheet": [], "total_score": 0}
                response_text = str(exam_payload.get("content", "")).strip()
                if exam_payload.get("_artifact_error"):
                    session_state = self._update_session_state(
                        session_state,
                        active_practice=None,
                        current_stage="practice_generation_failed",
                        current_step_index=3,
                        selected_memory=context_sections.get("memory_context", ""),
                        history_summary_state=history_summary_state,
                        tool_audit_refs=self._extract_tool_audit_refs(),
                    )
                    self._runtime_update_state_ref(state, session_state)
                    tool_calls = None
                else:
                    if response_text.startswith("# "):
                        response_text = response_text.replace("模拟考试试卷", "练习题（多题）", 1)
                    practice_artifact = self._practice_artifact_from_exam_payload(
                        exam_payload,
                        topic=topic,
                        question_type=question_type,
                    )
                    session_state = self._update_session_state(
                        session_state,
                        last_exam={
                            "answer_sheet": exam_payload.get("answer_sheet", []),
                            "total_score": int(exam_payload.get("total_score", 0)),
                            "content": response_text,
                        },
                        active_practice=practice_artifact.model_dump(),
                        current_stage="practice_generated",
                        current_step_index=3,
                        selected_memory=context_sections.get("memory_context", ""),
                        history_summary_state=history_summary_state,
                        tool_audit_refs=self._extract_tool_audit_refs(),
                    )
                    self._runtime_update_state_ref(state, session_state)
                    tool_calls = self._build_exam_meta_tool_call(
                        exam_payload.get("answer_sheet", []),
                        int(exam_payload.get("total_score", 0)),
                    )
            else:
                quiz = self.quizmaster.generate_quiz(
                    course_name=course_name,
                    topic=topic,
                    difficulty=difficulty,
                    context=context,
                    rag_context=context_sections["rag_context"],
                    history_context=context_sections["history_context"],
                    memory_context=context_sections["memory_context"],
                    retrieval_empty=retrieval_empty,
                    prefetched_memory_ctx=context_sections["memory_context"],
                    prefetched_memory_checked=True,
                    num_questions=num_questions,
                    question_type=question_type,
                )
                response_text = self._render_quiz_message(quiz)
                practice_artifact = self._practice_artifact_from_quiz(
                    quiz,
                    topic=topic,
                    question_type=question_type,
                )
                session_state = self._update_session_state(
                    session_state,
                    last_quiz=quiz.model_dump(),
                    active_practice=practice_artifact.model_dump(),
                    current_stage="practice_generated",
                    current_step_index=3,
                    selected_memory=context_sections.get("memory_context", ""),
                    history_summary_state=history_summary_state,
                    tool_audit_refs=self._extract_tool_audit_refs(),
                )
                self._runtime_update_state_ref(state, session_state)
                tool_calls = self._build_quiz_meta_tool_call(quiz)
            tool_calls = self._merge_internal_tool_calls(tool_calls, self._build_history_summary_tool_call(history_summary_state))

        self._last_run_meta = {
            "mode": "practice",
            "retrieval_empty": retrieval_empty,
            "answer_submission": answer_submission,
            "has_side_effect": answer_submission,
        }
        self._runtime_update_state_ref(state, session_state)
        if not runtime_managed:
            self._persist_session_state(session_state)

        return ChatMessage(
            role="assistant",
            content=response_text,
            citations=citations if citations else None,
            tool_calls=self._final_internal_tool_calls(
                session_state=session_state,
                history_summary_state=history_summary_state,
                extra_tool_calls=tool_calls,
            ),
        )

    def run_practice_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None,
        state: Dict[str, Any] = None,
    ):
        """对话式练习模式（流式）：交卷走 Grader 流式，出题走 QuizMaster。"""
        if history is None:
            history = []
        state = state or {}
        runtime_managed = self._runtime_managed(state)
        route_override = self._runtime_route_override(state)
        retrieval_empty = False
        retrieval_query = self._plan_retrieval_query(plan, user_message)
        memory_query = self._plan_memory_query(plan, user_message)
        agent_context = self._coerce_agent_context(state.get("agent_context"))
        if agent_context is not None:
            session_state = agent_context.session_snapshot
            history_summary_state = dict(session_state.history_summary_state or {})
            context_sections = {
                "history_context": agent_context.history_context,
                "rag_context": agent_context.rag_context,
                "memory_context": agent_context.memory_context,
                "context": agent_context.merged_context,
            }
            context = agent_context.merged_context
            citations_dicts = [c.model_dump() for c in agent_context.citations]
            payload = dict(agent_context.metadata.get("context_budget", {}) or {})
            retrieval_empty = bool(agent_context.metadata.get("retrieval_empty", False))
        else:
            history_summary_state, pending_history, recent_history, history_metrics = self._prepare_history_summary_inputs(history)
            session_state = self._extract_session_state(
                history=history,
                course_name=course_name,
                mode_hint="practice",
                user_message=user_message,
                state=state,
            )
            yield self.event_bus.status("正在检索教材证据...")
            rag_text, citations, retrieval_empty = self.rag_service.retrieve(
                course_name=course_name,
                question_raw=str(getattr(plan, "question_raw", "") or user_message),
                retrieval_query=retrieval_query,
                mode="practice",
                need_rag=plan.need_rag,
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于已有上下文出题）",
            )
            citations_dicts = [c.model_dump() for c in citations]

        if citations_dicts:
            yield self.event_bus.citations(citations_dicts)

        answer_submission = route_override == "run_grade" or (
            not route_override and self._is_answer_submission(user_message, history)
        )
        session_state = self._update_session_state(
            session_state,
            resolved_mode=str(getattr(plan, "resolved_mode", "") or "practice"),
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            user_intent=str(getattr(plan, "user_intent", "") or user_message),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="practice_grading" if answer_submission else "practice_generating",
            current_step_index=2 if answer_submission else 1,
            history_summary_state=history_summary_state,
        )
        self._runtime_update_state_ref(state, session_state)
        mem_agent = "grader" if answer_submission else "quizzer"
        mem_phase = "grade" if answer_submission else "generate"
        if agent_context is None:
            yield self.event_bus.status("正在检索历史记忆...")
            history_ctx = self._fetch_history_ctx(
                query=memory_query,
                course_name=course_name,
                mode="practice",
                agent=mem_agent,
                phase=mem_phase,
            )
            agent_context, packed, context_sections = self._build_agent_context(
                session_state=session_state,
                history=history,
                retrieval_query=retrieval_query,
                rag_text=rag_text,
                memory_text=history_ctx,
                citations=citations,
                mode="practice",
                history_summary_state=history_summary_state,
                pending_history=pending_history,
                recent_history=recent_history,
                history_metrics=history_metrics,
            )
            agent_context.metadata["retrieval_empty"] = retrieval_empty
            payload = self._context_budget_payload("practice", len(history), packed)
            context = agent_context.merged_context
        self.logger.info(
            "[context_budget_emit] mode=practice ratio=%.3f%s",
            float(payload.get("context_pressure_ratio", 0.0) or 0.0),
            self._trace_tag(),
        )
        yield self.event_bus.context_budget(payload)
        history_ctx = context_sections["memory_context"]
        self._init_tool_runtime_context(
            course_name=course_name,
            session_state=session_state,
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            taskgraph_step=route_override or ("run_grade" if answer_submission else "run_quiz"),
        )

        if answer_submission:
            yield self.event_bus.status("正在批改练习答案...")
            collected = []
            active_practice = session_state.active_practice
            has_exam_meta = bool(active_practice and len(list(active_practice.get("questions", []) or [])) > 1)
            if not has_exam_meta:
                has_exam_meta = bool(self._extract_internal_meta(history, "exam_meta"))
            if has_exam_meta:
                self.logger.info("[route] practice_stream answer_submission=1 target=grader_exam%s", self._trace_tag())
                exam_paper = self._practice_content_from_artifact(active_practice) or self._extract_exam_from_history(history)
                grade_history_ctx = self._dedupe_grading_history_ctx(history_ctx, exam_paper)
                for chunk in self.grader.grade_exam_stream(
                    exam_paper=exam_paper,
                    student_answer=user_message,
                    course_name=course_name,
                    history_ctx=grade_history_ctx,
                    retrieval_empty=retrieval_empty,
                ):
                    if isinstance(chunk, str):
                        collected.append(chunk)
                    yield chunk
            else:
                self.logger.info("[route] practice_stream answer_submission=1 target=grader%s", self._trace_tag())
                quiz_content = self._practice_content_from_artifact(active_practice) or self._extract_quiz_from_history(history)
                grade_history_ctx = self._dedupe_grading_history_ctx(history_ctx, quiz_content)
                for chunk in self.grader.grade_practice_stream(
                    quiz_content=quiz_content,
                    student_answer=user_message,
                    course_name=course_name,
                    history_ctx=grade_history_ctx,
                    retrieval_empty=retrieval_empty,
                ):
                    if isinstance(chunk, str):
                        collected.append(chunk)
                    yield chunk
            full_response = "".join(collected)
            if runtime_managed:
                effects = self._runtime_effects(state)
                effects["persist_records"] = {
                    "kind": "practice_record",
                    "course_name": course_name,
                    "user_message": user_message,
                    "history": list(history),
                    "response_text": full_response,
                }
                effects["persist_memory"] = {
                    "kind": "practice_grade",
                    "course_name": course_name,
                    "user_answer": user_message,
                    "history": list(history),
                    "response_text": full_response,
                }
                effects["record_notice"] = "practice"
            else:
                saved_path = self._save_practice_record(course_name, user_message, history, full_response)
                self._save_grading_to_memory(course_name, user_message, history, full_response)
                yield f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"
            session_state = self._update_session_state(
                session_state,
                current_stage="practice_graded",
                current_step_index=3,
                selected_memory=context_sections.get("memory_context", ""),
                history_summary_state=history_summary_state,
                tool_audit_refs=self._extract_tool_audit_refs(),
                latest_submission={
                    "artifact_kind": "practice",
                    "source_message": user_message,
                    "session_id": session_state.session_id,
                },
                latest_grading={
                    "artifact_kind": "practice",
                    "report_text": full_response,
                },
            )
            self._runtime_update_state_ref(state, session_state)
            if not runtime_managed:
                self._persist_session_state(session_state)
                yield self.event_bus.tool_calls(
                    self._final_internal_tool_calls(
                        session_state=session_state,
                        history_summary_state=history_summary_state,
                    )
                )
        else:
            yield self.event_bus.status("正在生成练习题...")
            topic, difficulty, num_questions, question_type = self._resolve_quiz_request(user_message)
            if route_override == "run_exam" or num_questions > 1:
                self.logger.info(
                    "[route] practice_stream multi_question=%d use_exam_generator=1%s",
                    num_questions,
                    self._trace_tag(),
                )
                practice_request = f"请生成{num_questions}道{question_type}练习题，主题：{topic}，难度：{difficulty}"
                exam_payload = self.quizmaster.generate_exam_paper(
                    course_name=course_name,
                    user_request=practice_request,
                    context=context,
                    rag_context=context_sections["rag_context"],
                    history_context=context_sections["history_context"],
                    memory_context=context_sections["memory_context"],
                    retrieval_empty=retrieval_empty,
                    prefetched_memory_ctx=context_sections["memory_context"],
                    prefetched_memory_checked=True,
                )
                if not isinstance(exam_payload, dict):
                    exam_payload = {"content": str(exam_payload), "answer_sheet": [], "total_score": 0}
                content = str(exam_payload.get("content", "")).strip()
                if exam_payload.get("_artifact_error"):
                    session_state = self._update_session_state(
                        session_state,
                        active_practice=None,
                        current_stage="practice_generation_failed",
                        current_step_index=3,
                        selected_memory=context_sections.get("memory_context", ""),
                        history_summary_state=history_summary_state,
                        tool_audit_refs=self._extract_tool_audit_refs(),
                    )
                    self._runtime_update_state_ref(state, session_state)
                    if not runtime_managed:
                        self._persist_session_state(session_state)
                        yield self.event_bus.tool_calls(
                            self._final_internal_tool_calls(
                                session_state=session_state,
                                history_summary_state=history_summary_state,
                            )
                        )
                else:
                    session_state = self._update_session_state(
                        session_state,
                        last_exam={
                            "answer_sheet": exam_payload.get("answer_sheet", []),
                            "total_score": int(exam_payload.get("total_score", 0)),
                            "content": content,
                        },
                        active_practice=self._practice_artifact_from_exam_payload(
                            exam_payload,
                            topic=topic,
                            question_type=question_type,
                        ).model_dump(),
                        current_stage="practice_generated",
                        current_step_index=3,
                        selected_memory=context_sections.get("memory_context", ""),
                        history_summary_state=history_summary_state,
                        tool_audit_refs=self._extract_tool_audit_refs(),
                    )
                    self._runtime_update_state_ref(state, session_state)
                    if not runtime_managed:
                        self._persist_session_state(session_state)
                        yield self.event_bus.tool_calls(
                            self._final_internal_tool_calls(
                                session_state=session_state,
                                history_summary_state=history_summary_state,
                                extra_tool_calls=self._build_exam_meta_tool_call(
                                    exam_payload.get("answer_sheet", []),
                                    int(exam_payload.get("total_score", 0)),
                                ),
                            )
                        )
                    else:
                        self._runtime_effects(state)["final_tool_calls"] = self._build_exam_meta_tool_call(
                            exam_payload.get("answer_sheet", []),
                            int(exam_payload.get("total_score", 0)),
                        )
                    if content.startswith("# "):
                        content = content.replace("模拟考试试卷", "练习题（多题）", 1)
                yield content
            else:
                quiz = self.quizmaster.generate_quiz(
                    course_name=course_name,
                    topic=topic,
                    difficulty=difficulty,
                    context=context,
                    rag_context=context_sections["rag_context"],
                    history_context=context_sections["history_context"],
                    memory_context=context_sections["memory_context"],
                    retrieval_empty=retrieval_empty,
                    prefetched_memory_ctx=context_sections["memory_context"],
                    prefetched_memory_checked=True,
                    num_questions=num_questions,
                    question_type=question_type,
                )
                session_state = self._update_session_state(
                    session_state,
                    last_quiz=quiz.model_dump(),
                    active_practice=self._practice_artifact_from_quiz(
                        quiz,
                        topic=topic,
                        question_type=question_type,
                    ).model_dump(),
                    current_stage="practice_generated",
                    current_step_index=3,
                    selected_memory=context_sections.get("memory_context", ""),
                    history_summary_state=history_summary_state,
                    tool_audit_refs=self._extract_tool_audit_refs(),
                )
                self._runtime_update_state_ref(state, session_state)
                if not runtime_managed:
                    self._persist_session_state(session_state)
                    yield self.event_bus.tool_calls(
                        self._final_internal_tool_calls(
                            session_state=session_state,
                            history_summary_state=history_summary_state,
                            extra_tool_calls=self._build_quiz_meta_tool_call(quiz),
                        )
                    )
                else:
                    self._runtime_effects(state)["final_tool_calls"] = self._build_quiz_meta_tool_call(quiz)
                yield self._render_quiz_message(quiz)

        self._last_run_meta = {
            "mode": "practice",
            "retrieval_empty": retrieval_empty,
            "answer_submission": answer_submission,
            "has_side_effect": answer_submission,
        }

    
    def run_exam_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: list = None,
        state: Dict[str, Any] = None,
    ) -> ChatMessage:
        """对话式考试模式（非流式）：出卷走 QuizMaster，交卷评分讲解走 Grader。"""
        if history is None:
            history = []
        state = state or {}
        runtime_managed = self._runtime_managed(state)
        route_override = self._runtime_route_override(state)
        retrieval_empty = False
        retrieval_query = self._plan_retrieval_query(plan, user_message)
        memory_query = self._plan_memory_query(plan, user_message)
        agent_context = self._coerce_agent_context(state.get("agent_context"))
        if agent_context is not None:
            session_state = agent_context.session_snapshot
            history_summary_state = dict(session_state.history_summary_state or {})
            citations = list(agent_context.citations or [])
            context_sections = {
                "history_context": agent_context.history_context,
                "rag_context": agent_context.rag_context,
                "memory_context": agent_context.memory_context,
                "context": agent_context.merged_context,
            }
            context = agent_context.merged_context
            retrieval_empty = bool(agent_context.metadata.get("retrieval_empty", False))
        else:
            history_summary_state, pending_history, recent_history, history_metrics = self._prepare_history_summary_inputs(history)
            session_state = self._extract_session_state(
                history=history,
                course_name=course_name,
                mode_hint="exam",
                user_message=user_message,
                state=state,
            )
            rag_text, citations, retrieval_empty = self.rag_service.retrieve(
                course_name=course_name,
                question_raw=str(getattr(plan, "question_raw", "") or user_message),
                retrieval_query=retrieval_query,
                mode="exam",
                need_rag=True,
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于已有上下文生成试卷/评分）",
            )

        answer_submission = route_override == "run_grade" or (
            not route_override and self._is_exam_answer_submission(user_message, history)
        )
        session_state = self._update_session_state(
            session_state,
            resolved_mode=str(getattr(plan, "resolved_mode", "") or "exam"),
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            user_intent=str(getattr(plan, "user_intent", "") or user_message),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="exam_grading" if answer_submission else "exam_generating",
            current_step_index=2 if answer_submission else 1,
            history_summary_state=history_summary_state,
        )
        self._runtime_update_state_ref(state, session_state)
        mem_agent = "grader" if answer_submission else "quizzer"
        mem_phase = "grade" if answer_submission else "generate"
        if agent_context is None:
            history_ctx = self._fetch_history_ctx(
                query=memory_query,
                course_name=course_name,
                mode="exam",
                agent=mem_agent,
                phase=mem_phase,
            )
            agent_context, packed, context_sections = self._build_agent_context(
                session_state=session_state,
                history=history,
                retrieval_query=retrieval_query,
                rag_text=rag_text,
                memory_text=history_ctx,
                citations=citations,
                mode="exam",
                history_summary_state=history_summary_state,
                pending_history=pending_history,
                recent_history=recent_history,
                history_metrics=history_metrics,
            )
            agent_context.metadata["retrieval_empty"] = retrieval_empty
            context = agent_context.merged_context
        history_ctx = context_sections["memory_context"]
        self._init_tool_runtime_context(
            course_name=course_name,
            session_state=session_state,
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            taskgraph_step=route_override or ("run_grade" if answer_submission else "run_exam"),
        )
        tool_calls = None
        if answer_submission:
            exam_paper = self._exam_content_from_artifact(session_state.active_exam) or self._extract_exam_from_history(history)
            grade_history_ctx = self._dedupe_grading_history_ctx(history_ctx, exam_paper)
            chunks = []
            for chunk in self.grader.grade_exam_stream(
                exam_paper=exam_paper,
                student_answer=user_message,
                course_name=course_name,
                history_ctx=grade_history_ctx,
                retrieval_empty=retrieval_empty,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            response_text = "".join(chunks)
            if runtime_managed:
                effects = self._runtime_effects(state)
                effects["persist_records"] = {
                    "kind": "exam_record",
                    "course_name": course_name,
                    "user_message": user_message,
                    "history": list(history),
                    "response_text": response_text,
                }
                effects["persist_memory"] = {
                    "kind": "exam_grade",
                    "course_name": course_name,
                    "response_text": response_text,
                }
                effects["record_notice"] = "exam"
            else:
                saved_path = self._save_exam_record(course_name, user_message, history, response_text)
                self._save_exam_to_memory(course_name, response_text)
                response_text += f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"
            session_state = self._update_session_state(
                session_state,
                current_stage="exam_graded",
                current_step_index=3,
                selected_memory=context_sections.get("memory_context", ""),
                history_summary_state=history_summary_state,
                tool_audit_refs=self._extract_tool_audit_refs(),
                latest_submission={
                    "artifact_kind": "exam",
                    "source_message": user_message,
                    "session_id": session_state.session_id,
                },
                latest_grading={
                    "artifact_kind": "exam",
                    "report_text": response_text,
                },
            )
            self._runtime_update_state_ref(state, session_state)
            tool_calls = None
        else:
            self.logger.info("[status] exam generating_paper%s", self._trace_tag())
            exam_payload = self.quizmaster.generate_exam_paper(
                course_name=course_name,
                user_request=user_message,
                context=context,
                rag_context=context_sections["rag_context"],
                history_context=context_sections["history_context"],
                memory_context=context_sections["memory_context"],
                retrieval_empty=retrieval_empty,
                prefetched_memory_ctx=context_sections["memory_context"],
                prefetched_memory_checked=True,
            )
            if not isinstance(exam_payload, dict):
                exam_payload = {"content": str(exam_payload), "answer_sheet": [], "total_score": 0}
            response_text = str(exam_payload.get("content", "")).strip()
            if exam_payload.get("_artifact_error"):
                session_state = self._update_session_state(
                    session_state,
                    active_exam=None,
                    current_stage="exam_generation_failed",
                    current_step_index=3,
                    selected_memory=context_sections.get("memory_context", ""),
                    history_summary_state=history_summary_state,
                    tool_audit_refs=self._extract_tool_audit_refs(),
                )
                self._runtime_update_state_ref(state, session_state)
                tool_calls = self._build_history_summary_tool_call(history_summary_state)
            else:
                session_state = self._update_session_state(
                    session_state,
                    last_exam={
                        "answer_sheet": exam_payload.get("answer_sheet", []),
                        "total_score": int(exam_payload.get("total_score", 0)),
                        "content": response_text,
                    },
                    active_exam=self._exam_artifact_from_payload(exam_payload).model_dump(),
                    current_stage="exam_generated",
                    current_step_index=3,
                    selected_memory=context_sections.get("memory_context", ""),
                    history_summary_state=history_summary_state,
                    tool_audit_refs=self._extract_tool_audit_refs(),
                )
                self._runtime_update_state_ref(state, session_state)
                tool_calls = self._build_exam_meta_tool_call(
                    exam_payload.get("answer_sheet", []),
                    int(exam_payload.get("total_score", 0)),
                )
                tool_calls = self._merge_internal_tool_calls(
                    tool_calls,
                    self._build_history_summary_tool_call(history_summary_state),
                )

        self._last_run_meta = {
            "mode": "exam",
            "retrieval_empty": retrieval_empty,
            "exam_grading": answer_submission,
            "has_side_effect": answer_submission,
        }
        self._runtime_update_state_ref(state, session_state)
        if not runtime_managed:
            self._persist_session_state(session_state)

        return ChatMessage(
            role="assistant",
            content=response_text,
            citations=citations if citations else None,
            tool_calls=self._final_internal_tool_calls(
                session_state=session_state,
                history_summary_state=history_summary_state,
                extra_tool_calls=tool_calls,
            ),
        )

    def run_exam_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: list = None,
        state: Dict[str, Any] = None,
    ):
        """对话式考试模式（流式）：交卷走 Grader 流式，出卷走 QuizMaster。"""
        if history is None:
            history = []
        state = state or {}
        runtime_managed = self._runtime_managed(state)
        route_override = self._runtime_route_override(state)
        retrieval_empty = False
        retrieval_query = self._plan_retrieval_query(plan, user_message)
        memory_query = self._plan_memory_query(plan, user_message)
        agent_context = self._coerce_agent_context(state.get("agent_context"))
        if agent_context is not None:
            session_state = agent_context.session_snapshot
            history_summary_state = dict(session_state.history_summary_state or {})
            context_sections = {
                "history_context": agent_context.history_context,
                "rag_context": agent_context.rag_context,
                "memory_context": agent_context.memory_context,
                "context": agent_context.merged_context,
            }
            context = agent_context.merged_context
            citations_dicts = [c.model_dump() for c in agent_context.citations]
            payload = dict(agent_context.metadata.get("context_budget", {}) or {})
            retrieval_empty = bool(agent_context.metadata.get("retrieval_empty", False))
        else:
            history_summary_state, pending_history, recent_history, history_metrics = self._prepare_history_summary_inputs(history)
            session_state = self._extract_session_state(
                history=history,
                course_name=course_name,
                mode_hint="exam",
                user_message=user_message,
                state=state,
            )
            yield self.event_bus.status("正在检索教材证据...")
            rag_text, citations, retrieval_empty = self.rag_service.retrieve(
                course_name=course_name,
                question_raw=str(getattr(plan, "question_raw", "") or user_message),
                retrieval_query=retrieval_query,
                mode="exam",
                need_rag=True,
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于已有上下文生成试卷/评分）",
            )
            citations_dicts = [c.model_dump() for c in citations]

        if citations_dicts:
            yield self.event_bus.citations(citations_dicts)

        answer_submission = route_override == "run_grade" or (
            not route_override and self._is_exam_answer_submission(user_message, history)
        )
        session_state = self._update_session_state(
            session_state,
            resolved_mode=str(getattr(plan, "resolved_mode", "") or "exam"),
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            user_intent=str(getattr(plan, "user_intent", "") or user_message),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="exam_grading" if answer_submission else "exam_generating",
            current_step_index=2 if answer_submission else 1,
            history_summary_state=history_summary_state,
        )
        self._runtime_update_state_ref(state, session_state)
        mem_agent = "grader" if answer_submission else "quizzer"
        mem_phase = "grade" if answer_submission else "generate"
        if agent_context is None:
            yield self.event_bus.status("正在检索历史记忆...")
            history_ctx = self._fetch_history_ctx(
                query=memory_query,
                course_name=course_name,
                mode="exam",
                agent=mem_agent,
                phase=mem_phase,
            )
            agent_context, packed, context_sections = self._build_agent_context(
                session_state=session_state,
                history=history,
                retrieval_query=retrieval_query,
                rag_text=rag_text,
                memory_text=history_ctx,
                citations=citations,
                mode="exam",
                history_summary_state=history_summary_state,
                pending_history=pending_history,
                recent_history=recent_history,
                history_metrics=history_metrics,
            )
            agent_context.metadata["retrieval_empty"] = retrieval_empty
            payload = self._context_budget_payload("exam", len(history), packed)
            context = agent_context.merged_context
        self.logger.info(
            "[context_budget_emit] mode=exam ratio=%.3f%s",
            float(payload.get("context_pressure_ratio", 0.0) or 0.0),
            self._trace_tag(),
        )
        yield self.event_bus.context_budget(payload)
        history_ctx = context_sections["memory_context"]
        self._init_tool_runtime_context(
            course_name=course_name,
            session_state=session_state,
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            question_raw=str(getattr(plan, "question_raw", "") or user_message),
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            taskgraph_step=route_override or ("run_grade" if answer_submission else "run_exam"),
        )
        if answer_submission:
            yield self.event_bus.status("正在批改考试答案...")
            exam_paper = self._exam_content_from_artifact(session_state.active_exam) or self._extract_exam_from_history(history)
            grade_history_ctx = self._dedupe_grading_history_ctx(history_ctx, exam_paper)
            collected = []
            for chunk in self.grader.grade_exam_stream(
                exam_paper=exam_paper,
                student_answer=user_message,
                course_name=course_name,
                history_ctx=grade_history_ctx,
                retrieval_empty=retrieval_empty,
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
                yield chunk
            full_response = "".join(collected)
            if runtime_managed:
                effects = self._runtime_effects(state)
                effects["persist_records"] = {
                    "kind": "exam_record",
                    "course_name": course_name,
                    "user_message": user_message,
                    "history": list(history),
                    "response_text": full_response,
                }
                effects["persist_memory"] = {
                    "kind": "exam_grade",
                    "course_name": course_name,
                    "response_text": full_response,
                }
                effects["record_notice"] = "exam"
            else:
                saved_path = self._save_exam_record(course_name, user_message, history, full_response)
                self._save_exam_to_memory(course_name, full_response)
                yield f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"
            session_state = self._update_session_state(
                session_state,
                current_stage="exam_graded",
                current_step_index=3,
                selected_memory=context_sections.get("memory_context", ""),
                history_summary_state=history_summary_state,
                tool_audit_refs=self._extract_tool_audit_refs(),
                latest_submission={
                    "artifact_kind": "exam",
                    "source_message": user_message,
                    "session_id": session_state.session_id,
                },
                latest_grading={
                    "artifact_kind": "exam",
                    "report_text": full_response,
                },
            )
            self._runtime_update_state_ref(state, session_state)
            if not runtime_managed:
                self._persist_session_state(session_state)
                yield self.event_bus.tool_calls(
                    self._final_internal_tool_calls(
                        session_state=session_state,
                        history_summary_state=history_summary_state,
                    )
                )
        else:
            yield self.event_bus.status("正在生成考试试卷...")
            exam_payload = self.quizmaster.generate_exam_paper(
                course_name=course_name,
                user_request=user_message,
                context=context,
                rag_context=context_sections["rag_context"],
                history_context=context_sections["history_context"],
                memory_context=context_sections["memory_context"],
                retrieval_empty=retrieval_empty,
                prefetched_memory_ctx=context_sections["memory_context"],
                prefetched_memory_checked=True,
            )
            if not isinstance(exam_payload, dict):
                exam_payload = {"content": str(exam_payload), "answer_sheet": [], "total_score": 0}
            content = str(exam_payload.get("content", "")).strip()
            if exam_payload.get("_artifact_error"):
                session_state = self._update_session_state(
                    session_state,
                    active_exam=None,
                    current_stage="exam_generation_failed",
                    current_step_index=3,
                    selected_memory=context_sections.get("memory_context", ""),
                    history_summary_state=history_summary_state,
                    tool_audit_refs=self._extract_tool_audit_refs(),
                )
                self._runtime_update_state_ref(state, session_state)
                if not runtime_managed:
                    self._persist_session_state(session_state)
                    yield self.event_bus.tool_calls(
                        self._final_internal_tool_calls(
                            session_state=session_state,
                            history_summary_state=history_summary_state,
                        )
                    )
            else:
                session_state = self._update_session_state(
                    session_state,
                    last_exam={
                        "answer_sheet": exam_payload.get("answer_sheet", []),
                        "total_score": int(exam_payload.get("total_score", 0)),
                        "content": content,
                    },
                    active_exam=self._exam_artifact_from_payload(exam_payload).model_dump(),
                    current_stage="exam_generated",
                    current_step_index=3,
                    selected_memory=context_sections.get("memory_context", ""),
                    history_summary_state=history_summary_state,
                    tool_audit_refs=self._extract_tool_audit_refs(),
                )
                self._runtime_update_state_ref(state, session_state)
                if not runtime_managed:
                    self._persist_session_state(session_state)
                    yield self.event_bus.tool_calls(
                        self._final_internal_tool_calls(
                            session_state=session_state,
                            history_summary_state=history_summary_state,
                            extra_tool_calls=self._build_exam_meta_tool_call(
                                exam_payload.get("answer_sheet", []),
                                int(exam_payload.get("total_score", 0)),
                            ),
                        )
                    )
                else:
                    self._runtime_effects(state)["final_tool_calls"] = self._build_exam_meta_tool_call(
                        exam_payload.get("answer_sheet", []),
                        int(exam_payload.get("total_score", 0)),
                    )
            yield content

        self._last_run_meta = {
            "mode": "exam",
            "retrieval_empty": retrieval_empty,
            "exam_grading": answer_submission,
            "has_side_effect": answer_submission,
        }

    def _save_mistake(
        self,
        course_name: str,
        quiz: Quiz,
        student_answer: str,
        grade_report: GradeReport
    ):
        """Save mistake to log."""
        self.workspace_store.save_mistake(course_name, quiz, student_answer, grade_report)

    # ------------------------------------------------------------------ #
    #  记录检测 & 自动保存辅助方法
    # ------------------------------------------------------------------ #

    def _check_quality(self, content: str) -> bool:
        """检查 Tutor 回答质量，过短或含错误信号则返回 False。"""
        if not content or len(content.strip()) < 150:
            return False
        error_signals = ["Error calling LLM", "工具调用失败", "请重试", "API Error"]
        return not any(sig in content for sig in error_signals)

    def _fetch_history_ctx(
        self,
        query: str,
        course_name: str,
        mode: str = "",
        agent: str = "",
        phase: str = "",
    ) -> str:
        """从记忆库预取历史错题片段，返回可追加到 system prompt 的字符串。"""
        return self.memory_service.prefetch_history_ctx(
            query=query,
            course_name=course_name,
            mode=mode,
            agent=agent,
            phase=phase,
        )

    @staticmethod
    def _context_sections_from_packed(packed: Dict[str, Any]) -> Dict[str, str]:
        history_text = str(packed.get("history_text", "") or "").strip()
        rag_text = str(packed.get("rag_text", "") or "").strip()
        memory_text = str(packed.get("memory_text", "") or "").strip()
        final_text = str(packed.get("final_text", "") or "").strip()
        return {
            "history_context": history_text,
            "rag_context": rag_text,
            "memory_context": memory_text,
            "context": final_text,
        }

    @staticmethod
    def _log_context_budget(mode: str, packed: Dict[str, Any]) -> None:
        history_tokens = int(packed.get("history_tokens_est", 0) or 0)
        rag_tokens = int(packed.get("rag_tokens_est", 0) or 0)
        memory_tokens = int(packed.get("memory_tokens_est", 0) or 0)
        final_tokens = int(packed.get("final_tokens_est", 0) or 0)
        budget_tokens = int(packed.get("budget_tokens_est", 0) or 0)
        logging.getLogger("runner").info(
            "[context_budget] mode=%s history=%d rag=%d memory=%d final=%d budget=%d",
            mode,
            history_tokens,
            rag_tokens,
            memory_tokens,
            final_tokens,
            budget_tokens,
        )

    @staticmethod
    def _context_budget_payload(mode: str, history_len: int, packed: Dict[str, Any]) -> Dict[str, Any]:
        history_tokens = int(packed.get("history_tokens_est", 0) or 0)
        rag_tokens = int(packed.get("rag_tokens_est", 0) or 0)
        memory_tokens = int(packed.get("memory_tokens_est", 0) or 0)
        final_tokens = int(packed.get("final_tokens_est", 0) or 0)
        budget_tokens = int(packed.get("budget_tokens_est", 0) or 0)
        ratio = 0.0
        if budget_tokens > 0:
            ratio = max(0.0, min(1.0, float(final_tokens) / float(budget_tokens)))
        return {
            "mode": mode,
            "history_len": int(history_len or 0),
            "history_tokens_est": history_tokens,
            "rag_tokens_est": rag_tokens,
            "memory_tokens_est": memory_tokens,
            "final_tokens_est": final_tokens,
            "budget_tokens_est": budget_tokens,
            "context_pressure_ratio": ratio,
            "history_summary_source": str(packed.get("history_summary_source", "none") or "none"),
            "history_summary_block_count": int(packed.get("history_summary_block_count", 0) or 0),
            "history_summary_state_hit": bool(packed.get("history_summary_state_hit", False)),
            "history_llm_compress_applied": bool(packed.get("history_llm_compress_applied", False)),
            "history_llm_compress_ms": packed.get("history_llm_compress_ms"),
            "hard_truncated": bool(packed.get("hard_truncated", False)),
        }

    """从用户出题请求中解析主题、难度、题量与题型（轻量规则，失败回退默认值）。"""
    @staticmethod
    def _resolve_quiz_request(user_message: str) -> tuple[str, str, int, str]:
        import re

        text = (user_message or "").strip()
        if not text:
            return "当前课程核心知识点", "medium", 1, "简答题"

        lowered = text.lower()
        difficulty = "medium"
        if any(k in text for k in ["简单", "基础", "入门"]) or "easy" in lowered:
            difficulty = "easy"
        elif any(k in text for k in ["困难", "综合", "挑战"]) or "hard" in lowered:
            difficulty = "hard"
        elif any(k in text for k in ["中等", "普通"]) or "medium" in lowered:
            difficulty = "medium"

        # 解析题量：支持阿拉伯数字与常见中文数字（默认 1，最大 20）。
        num_questions = 1
        count_match = re.search(r"(\d{1,2})\s*(?:道|题|个|条)", text)
        if count_match:
            num_questions = int(count_match.group(1))
        else:
            zh_map = {
                "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
                "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
                "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
            }
            for zh, value in sorted(zh_map.items(), key=lambda x: len(x[0]), reverse=True):
                if re.search(fr"{zh}\s*(?:道|题|个|条)", text):
                    num_questions = value
                    break
        num_questions = max(1, min(num_questions, 20))

        # 解析题型：未指定时交给命题器自动决定。
        question_type = "综合题"
        if any(k in text for k in ["判断题", "判断"]):
            question_type = "判断题"
        elif any(k in text for k in ["单选", "选择题", "多选"]):
            question_type = "选择题"
        elif "填空" in text:
            question_type = "填空题"
        elif "简答" in text:
            question_type = "简答题"
        elif "论述" in text:
            question_type = "论述题"
        elif "计算" in text:
            question_type = "计算题"

        topic = text
        # 去除常见口语前缀，保留核心主题短语
        for prefix in ["给我出", "帮我出", "请出", "出", "来", "我想练习"]:
            if topic.startswith(prefix):
                topic = topic[len(prefix):].strip()
        topic = re.sub(r"\d{1,2}\s*(?:道|题|个|条)", "", topic)
        topic = re.sub(r"(?:一|二|两|三|四|五|六|七|八|九|十|十一|十二|十三|十四|十五|十六|十七|十八|十九|二十)\s*(?:道|题|个|条)", "", topic)
        topic = re.sub(r"(判断题|选择题|单选题|多选题|填空题|简答题|论述题|计算题)", "", topic)
        topic = re.sub(r"^(关于|有关)", "", topic).strip()
        if topic.endswith("的") and len(topic) > 2:
            topic = topic[:-1].strip()
        topic = re.sub(r"[，,。；;!！]+$", "", topic).strip()
        topic = topic[:120] if topic else text[:120]
        return topic, difficulty, num_questions, question_type

    """将 Quiz 结构渲染为学生可见题面。"""
    @staticmethod
    def _render_quiz_message(quiz: Quiz) -> str:
        return (
            "## 练习题\n\n"
            f"{quiz.question}\n\n"
            "请回答上述题目，回答完毕后我会为你评分并给出详细讲解。"
        )

    @staticmethod
    def _practice_artifact_from_quiz(
        quiz: Quiz,
        *,
        topic: str = "",
        question_type: str = "综合题",
    ) -> PracticeArtifactV1:
        return PracticeArtifactV1(
            title="练习题",
            instructions="请回答上述题目，回答完毕后我会为你评分并给出详细讲解。",
            topic=topic,
            requested_num_questions=1,
            question_type=question_type,
            questions=[
                ArtifactQuestionV1(
                    id=1,
                    type=question_type,
                    question=quiz.question,
                    options=[],
                    score=100,
                    standard_answer=quiz.standard_answer,
                    rubric=quiz.rubric,
                    chapter=str(quiz.chapter or ""),
                    concept=str(quiz.concept or ""),
                    difficulty=quiz.difficulty,
                )
            ],
            total_score=100,
        )

    @staticmethod
    def _practice_artifact_from_exam_payload(
        exam_payload: Dict[str, Any],
        *,
        topic: str = "",
        question_type: str = "综合题",
    ) -> PracticeArtifactV1:
        questions: List[ArtifactQuestionV1] = []
        for idx, item in enumerate(list(exam_payload.get("answer_sheet", []) or []), start=1):
            if not isinstance(item, dict):
                continue
            questions.append(
                ArtifactQuestionV1(
                    id=int(item.get("id", idx) or idx),
                    type=str(item.get("type", question_type) or question_type),
                    question=str(item.get("question", "") or ""),
                    options=list(item.get("options", []) or []),
                    score=int(item.get("score", 0) or 0),
                    standard_answer=str(item.get("standard_answer", "") or ""),
                    rubric=str(item.get("rubric", "") or ""),
                    chapter=str(item.get("chapter", "") or ""),
                    concept=str(item.get("concept", "") or ""),
                    difficulty=str(item.get("difficulty", "medium") or "medium"),
                )
            )
        return PracticeArtifactV1(
            title="练习题（多题）",
            instructions="请将各题答案统一整理后一次性提交。",
            topic=topic,
            requested_num_questions=max(1, len(questions)),
            question_type=question_type,
            questions=questions,
            total_score=int(exam_payload.get("total_score", 0) or 0),
        )

    @staticmethod
    def _exam_artifact_from_payload(exam_payload: Dict[str, Any]) -> ExamArtifactV1:
        questions: List[ArtifactQuestionV1] = []
        for idx, item in enumerate(list(exam_payload.get("answer_sheet", []) or []), start=1):
            if not isinstance(item, dict):
                continue
            questions.append(
                ArtifactQuestionV1(
                    id=int(item.get("id", idx) or idx),
                    type=str(item.get("type", "综合题") or "综合题"),
                    question=str(item.get("question", "") or ""),
                    options=list(item.get("options", []) or []),
                    score=int(item.get("score", 0) or 0),
                    standard_answer=str(item.get("standard_answer", "") or ""),
                    rubric=str(item.get("rubric", "") or ""),
                    chapter=str(item.get("chapter", "") or ""),
                    concept=str(item.get("concept", "") or ""),
                    difficulty=str(item.get("difficulty", "medium") or "medium"),
                )
            )
        return ExamArtifactV1(
            title=str(exam_payload.get("title", "") or "模拟考试试卷"),
            instructions=str(exam_payload.get("instructions", "") or "请将各题答案统一整理后一次性提交。"),
            questions=questions,
            total_score=int(exam_payload.get("total_score", 0) or 0),
        )

    @staticmethod
    def _practice_content_from_artifact(artifact: Optional[Dict[str, Any]]) -> str:
        if not isinstance(artifact, dict):
            return ""
        content = str(artifact.get("content", "") or "").strip()
        if content:
            return content
        questions = list(artifact.get("questions", []) or [])
        if not questions:
            return ""
        lines = ["## 练习题", ""]
        for idx, item in enumerate(questions, start=1):
            if not isinstance(item, dict):
                continue
            lines.append(f"{idx}. {str(item.get('question', '') or '').strip()}")
            options = list(item.get("options", []) or [])
            if options:
                lines.append("")
                lines.extend(str(opt) for opt in options)
            lines.append("")
        lines.append(str(artifact.get("instructions", "") or "请回答上述题目，回答完毕后我会为你评分并给出详细讲解。"))
        return "\n".join(lines).strip()

    @staticmethod
    def _exam_content_from_artifact(artifact: Optional[Dict[str, Any]]) -> str:
        if not isinstance(artifact, dict):
            return ""
        content = str(artifact.get("content", "") or "").strip()
        if content:
            return content
        title = str(artifact.get("title", "") or "模拟考试试卷")
        instructions = str(artifact.get("instructions", "") or "请将各题答案统一整理后一次性提交。")
        questions = list(artifact.get("questions", []) or [])
        lines = [f"# {title}", "", f"**考试须知**：{instructions}", "", "---", ""]
        for idx, item in enumerate(questions, start=1):
            if not isinstance(item, dict):
                continue
            score = int(item.get("score", 0) or 0)
            lines.append(f"{idx}. {str(item.get('question', '') or '').strip()}（{score}分）")
            options = list(item.get("options", []) or [])
            if options:
                lines.append("")
                lines.extend(str(opt) for opt in options)
            lines.append("")
        lines.extend(["---", "", "✅ 请将各题答案统一整理后一次性提交。"])
        return "\n".join(lines).strip()

    @staticmethod
    def _normalize_overlap_text(text: str) -> str:
        import re

        return re.sub(r"\s+", "", str(text or "")).strip().lower()

    @classmethod
    def _dedupe_grading_history_ctx(cls, history_ctx: str, artifact_text: str) -> str:
        import re

        history_text = str(history_ctx or "").strip()
        artifact = str(artifact_text or "").strip()
        if not history_text or not artifact:
            return history_text

        norm_history = cls._normalize_overlap_text(history_text)
        norm_artifact = cls._normalize_overlap_text(artifact)
        if len(norm_artifact) < 40:
            return history_text

        if norm_history and norm_history in norm_artifact:
            return ""
        if norm_artifact in norm_history:
            extra_chars = len(norm_history) - len(norm_artifact)
            if extra_chars <= max(20, len(norm_artifact) // 5):
                return ""

        artifact_lines = {
            cls._normalize_overlap_text(line)
            for line in artifact.splitlines()
            if len(cls._normalize_overlap_text(line)) >= 12
        }
        if not artifact_lines:
            return history_text

        kept_blocks: List[str] = []
        for block in re.split(r"\n\s*\n", history_text):
            block_text = str(block or "").strip()
            if not block_text:
                continue
            norm_block = cls._normalize_overlap_text(block_text)
            if len(norm_block) >= 120 and (norm_block in norm_artifact or norm_artifact in norm_block):
                continue
            block_lines = [
                cls._normalize_overlap_text(line)
                for line in block_text.splitlines()
                if len(cls._normalize_overlap_text(line)) >= 12
            ]
            if block_lines and all(line in artifact_lines for line in block_lines) and len(norm_block) >= 20:
                continue
            if len(block_lines) >= 3:
                overlap_ratio = sum(1 for line in block_lines if line in artifact_lines) / len(block_lines)
                if overlap_ratio >= 0.6:
                    continue
            kept_blocks.append(block_text)
        return "\n\n".join(kept_blocks).strip()

    """将 Quiz 元数据挂载到内部 tool_calls，避免污染正文显示。"""
    @staticmethod
    def _build_quiz_meta_tool_call(quiz: Quiz) -> List[Dict[str, Any]]:
        return [
            {
                "type": "internal_meta",
                "name": "quiz_meta",
                "payload": {
                    "question": quiz.question,
                    "standard_answer": quiz.standard_answer,
                    "rubric": quiz.rubric,
                    "chapter": quiz.chapter,
                    "concept": quiz.concept,
                    "difficulty": quiz.difficulty,
                },
            }
        ]

    """将考试答案表挂载到内部 tool_calls，供交卷评分阶段使用。"""
    @staticmethod
    def _build_exam_meta_tool_call(answer_sheet: List[Dict[str, Any]], total_score: int) -> List[Dict[str, Any]]:
        return [
            {
                "type": "internal_meta",
                "name": "exam_meta",
                "payload": {
                    "answer_sheet": answer_sheet or [],
                    "total_score": int(total_score or 0),
                },
            }
        ]

    """从历史里提取内部元数据。"""
    @staticmethod
    def _extract_internal_meta(history: list, name: str) -> Optional[Dict[str, Any]]:
        for msg in reversed(history[-30:]):
            if msg.get("role") != "assistant":
                continue
            tcs = msg.get("tool_calls") or []
            if not isinstance(tcs, list):
                continue
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                if tc.get("type") == "internal_meta" and tc.get("name") == name:
                    payload = tc.get("payload")
                    if isinstance(payload, dict):
                        return payload
        return None

    def _is_answer_submission(self, user_message: str, history: list) -> bool:
        """检测用户是否在提交答案（而非请求出题）。

        判断依据：
        1. 用户消息含答案提交特征词或答案编号格式。
        2. 历史中最近一条 assistant 消息包含题目结构。
        """
        import re
        answer_markers = [
            "第1题", "第一题", "我的答案", "答案如下", "提交答案", "答：",
        ]
        has_answer_marker = any(m in user_message for m in answer_markers)
        # 纯编号+答案组合（如 "1.A  2.B  3.正确"）
        if re.search(r'[1-9][.、：:]\s*[A-Za-z正确错误√×对]', user_message):
            has_answer_marker = True
        # 多行都有 "第X题" 或 "X." 编号
        if len(re.findall(r'(?:第\d+题|^\d+[.、])', user_message, re.MULTILINE)) >= 2:
            has_answer_marker = True

        # 最近 assistant 消息是否含题目结构
        has_quiz_in_history = bool(self._extract_internal_meta(history, "quiz_meta")) or bool(
            self._extract_internal_meta(history, "exam_meta")
        )
        if not has_quiz_in_history:
            for msg in reversed(history[-12:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    quiz_signals = ["题目", "选择题", "判断题", "填空题", "简答题",
                                    "第1题", "第一题", "标准答案", "答案选", "下列哪", "以下哪"]
                    if sum(1 for kw in quiz_signals if kw in content) >= 2:
                        has_quiz_in_history = True
                    break

        if not has_quiz_in_history:
            return False

        # 放宽判定：已出题后，像“错误/正确/A”这类短答案也应进入评分阶段。
        normalized = (user_message or "").strip().lower()
        simple_answers = {
            "a", "b", "c", "d", "ab", "ac", "ad", "bc", "bd", "cd", "abcd",
            "对", "错", "正确", "错误", "true", "false", "√", "×",
        }
        if normalized in simple_answers:
            return True

        compact_answer = re.fullmatch(
            r"(?:[a-dA-D]|正确|错误|对|错|√|×)(?:[\s,，、;/；]+(?:[a-dA-D]|正确|错误|对|错|√|×)){0,19}",
            (user_message or "").strip(),
        )
        if compact_answer:
            return True

        request_like = any(
            k in user_message for k in [
                "出题", "再来", "下一题", "继续出", "给我出", "帮我出", "来一道",
                "练习题", "判断题", "选择题", "填空题", "简答题", "论述题", "计算题",
            ]
        )
        if re.search(
            r"(?:给我|帮我|请)?\s*(?:再)?出\s*(?:[一二两三四五六七八九十\d]+\s*)?(?:道|题|个|条)",
            user_message,
        ):
            request_like = True
        if re.search(r"(来|再来)\s*一\s*(?:道|题)", user_message):
            request_like = True
        if re.search(r"下一\s*题", user_message):
            request_like = True
        ask_like = any(
            k in user_message for k in ["什么", "为什么", "怎么", "解释", "提示", "再讲", "请讲"]
        ) or ("?" in user_message or "？" in user_message)

        if has_answer_marker:
            return True
        if len((user_message or "").strip()) <= 24 and not request_like and not ask_like:
            return True

        return False

    """检测考试模式是否为“提交答案”阶段。"""
    def _is_exam_answer_submission(self, user_message: str, history: list) -> bool:
        import re

        answer_markers = [
            "第1题", "第一题", "我的答案", "答案如下", "提交答案", "答：",
        ]
        has_answer_marker = any(m in user_message for m in answer_markers)
        if re.search(r'[1-9][.、：:]\s*[A-Za-z正确错误√×对]', user_message):
            has_answer_marker = True
        if len(re.findall(r'(?:第\d+题|^\d+[.、])', user_message, re.MULTILINE)) >= 2:
            has_answer_marker = True

        has_exam_in_history = bool(self._extract_internal_meta(history, "exam_meta"))
        if not has_exam_in_history:
            for msg in reversed(history[-20:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    exam_signals = ["模拟考试试卷", "考试须知", "第一部分", "总分"]
                    if sum(1 for kw in exam_signals if kw in content) >= 2:
                        has_exam_in_history = True
                    break
        return has_answer_marker and has_exam_in_history

    def _extract_quiz_from_history(self, history: list) -> str:
        """从历史中提取最近一条 assistant 出题消息作为题目原文。"""
        meta = self._extract_internal_meta(history, "quiz_meta")
        if meta:
            question = str(meta.get("question", "")).strip()
            standard_answer = str(meta.get("standard_answer", "")).strip()
            rubric = str(meta.get("rubric", "")).strip()
            chapter = str(meta.get("chapter", "")).strip()
            concept = str(meta.get("concept", "")).strip()
            difficulty = str(meta.get("difficulty", "")).strip()
            parts = [
                "【题目】",
                question or "（题干缺失）",
                "",
                "【标准答案】",
                standard_answer or "（标准答案缺失）",
                "",
                "【评分标准】",
                rubric or "（评分标准缺失）",
            ]
            if chapter:
                parts.extend(["", f"【章节】{chapter}"])
            if concept:
                parts.extend([f"【知识点】{concept}"])
            if difficulty:
                parts.extend([f"【难度】{difficulty}"])
            return "\n".join(parts)

        for msg in reversed(history[-12:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                quiz_signals = [
                    "练习题",
                    "题目",
                    "选择题",
                    "判断题",
                    "填空题",
                    "简答题",
                    "第1题",
                    "第一题",
                    "标准答案",
                    "答案选",
                    "下列哪",
                    "以下哪",
                    "请回答上述题目",
                    "回答完毕后我会为你评分",
                ]
                if sum(1 for kw in quiz_signals if kw in content) >= 1:
                    return content
        return "（未能从历史中提取题目，请检查对话上下文）"

    """从历史中提取最近一份考试试卷原文。"""
    def _extract_exam_from_history(self, history: list) -> str:
        meta = self._extract_internal_meta(history, "exam_meta")
        if meta:
            visible_exam = ""
            for msg in reversed(history[-20:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    signals = ["模拟考试试卷", "第一部分", "考试须知"]
                    if sum(1 for kw in signals if kw in content) >= 2:
                        visible_exam = content
                        break
            hidden_meta = {
                "answer_sheet": meta.get("answer_sheet", []) or [],
                "total_score": int(meta.get("total_score", 0) or 0),
            }
            if not visible_exam:
                visible_exam = "# 模拟考试试卷\n\n（未在历史中提取到试卷正文）"
            return (
                f"{visible_exam}\n\n"
                "[INTERNAL_EXAM_META]\n"
                f"{json.dumps(hidden_meta, ensure_ascii=False)}"
            )

        for msg in reversed(history[-20:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                signals = [
                    "模拟考试试卷",
                    "第一部分",
                    "考试须知",
                    "请将各题答案统一整理后一次性提交",
                    "一、",
                    "二、",
                    "三、",
                ]
                if sum(1 for kw in signals if kw in content) >= 1:
                    return content
        return "（未能从历史中提取试卷，请检查对话上下文）"

    def _is_practice_grading(self, text: str) -> bool:
        """判断练习模式回复是否为评分阶段。"""
        keywords = ["评分结果", "标准解析", "易错提醒", "得分", "答对的部分", "需要改进", "逐题核对", "标准答案", "学生答案"]
        return sum(1 for kw in keywords if kw in text) >= 2

    def _save_grading_to_memory(
        self,
        course_name: str,
        user_answer: str,
        history: list,
        response_text: str,
    ) -> None:
        """将练习评分结果写入情景记忆，使用 PracticeGradeSignal 提取结构化信息。"""
        try:
            self.memory_service.save_practice_grade(course_name, user_answer, history, response_text)
        except Exception as _e:
            self.logger.warning("[memory] practice_save_failed err=%s%s", str(_e), self._trace_tag())

    def _save_practice_record(self, course_name: str, user_message: str, history: list, response_text: str) -> str:
        """保存练习题记录（题目、用户答案、评分解析），返回相对路径。
        user_message: 当前用户提交的答案（直接传入，不从 history 提取）
        history: 当前消息之前的历史（用于提取题目内容）
        """
        return self.workspace_store.save_practice_record(course_name, user_message, history, response_text)

    def _save_exam_record(self, course_name: str, user_message: str, history: list, response_text: str) -> str:
        """保存考试完整记录（试卷、用户答案、批改报告），返回相对路径。
        user_message: 用户提交的全部答案（直接传入）
        history: 当前消息之前的历史（用于提取试卷内容）
        """
        return self.workspace_store.save_exam_record(course_name, user_message, history, response_text)

    def _save_exam_to_memory(self, course_name: str, response_text: str) -> None:
        """将考试批改结果写入情景记忆，并同步薄弱知识点到用户画像。"""
        try:
            self.memory_service.save_exam_grade(course_name, response_text)
        except Exception as _e:
            self.logger.warning("[memory] exam_save_failed err=%s%s", str(_e), self._trace_tag())

    def run(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None
    ) -> tuple[ChatMessage, PlanPlusV1]:
        """主编排入口（非流式），由 Runtime 编译 TaskGraph 并执行。"""
        self._tool_dedup_cache = {}
        response, plan, _graph = self.runtime.execute_sync(
            course_name=course_name,
            mode_hint=mode,
            user_message=user_message,
            state=state,
            history=history,
        )
        return response, plan

    def run_learn_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None,
        state: Dict[str, Any] = None,
    ):
        """流式学习模式：先检索上下文，再流式输出导师回答。

        首先 yield 一个特殊事件 {"__citations__": [...]} 供前端捕获并展示引用框。
        后续所有 yield 均为文本 chunk。
        """
        if history is None:
            history = []
        state = state or {}
        runtime_managed = self._runtime_managed(state)
        question_raw = self._plan_question_raw(plan, user_message)
        retrieval_query = self._plan_retrieval_query(plan, user_message)
        memory_query = self._plan_memory_query(plan, user_message)
        agent_context = self._coerce_agent_context(state.get("agent_context"))
        if agent_context is not None:
            session_state = agent_context.session_snapshot
            history_summary_state = dict(session_state.history_summary_state or {})
        else:
            history_summary_state, pending_history, recent_history, history_metrics = self._prepare_history_summary_inputs(history)
            session_state = self._extract_session_state(
                history=history,
                course_name=course_name,
                mode_hint="learn",
                user_message=user_message,
                state=state,
            )
        session_state = self._update_session_state(
            session_state,
            resolved_mode=str(getattr(plan, "resolved_mode", "") or "learn"),
            question_raw=question_raw,
            user_intent=str(getattr(plan, "user_intent", "") or question_raw),
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            current_stage="learn_running",
            current_step_index=1,
            history_summary_state=history_summary_state,
        )
        self._runtime_update_state_ref(state, session_state)

        if agent_context is not None:
            citations_dicts = [c.model_dump() for c in agent_context.citations]
            context_sections = {
                "history_context": agent_context.history_context,
                "rag_context": agent_context.rag_context,
                "memory_context": agent_context.memory_context,
                "context": agent_context.merged_context,
            }
            context = agent_context.merged_context
            payload = dict(agent_context.metadata.get("context_budget", {}) or {})
        else:
            yield self.event_bus.status("正在检索教材证据...")
            rag_text, citations, retrieval_empty = self.rag_service.retrieve(
                course_name=course_name,
                question_raw=question_raw,
                retrieval_query=retrieval_query,
                mode="learn",
                need_rag=plan.need_rag,
                missing_index_message="（未找到相关教材，请先上传课程资料）",
                empty_message="（检索未命中有效教材片段，本轮将基于通用知识和已有上下文回答）",
            )
            citations_dicts = [c.model_dump() for c in citations]
            yield self.event_bus.status("正在检索历史记忆...")
            memory_ctx = self._fetch_history_ctx(
                query=memory_query,
                course_name=course_name,
                mode="learn",
                agent="tutor",
                phase="answer",
            )
            agent_context, packed, context_sections = self._build_agent_context(
                session_state=session_state,
                history=history,
                retrieval_query=retrieval_query,
                rag_text=rag_text,
                memory_text=memory_ctx,
                citations=citations,
                mode="learn",
                history_summary_state=history_summary_state,
                pending_history=pending_history,
                recent_history=recent_history,
                history_metrics=history_metrics,
            )
            agent_context.metadata["retrieval_empty"] = retrieval_empty
            payload = self._context_budget_payload("learn", len(history), packed)
            context = agent_context.merged_context

        # 先发送 citations 事件（前端按 __citations__ key 识别，不会渲染为文本）
        if citations_dicts:
            yield self.event_bus.citations(citations_dicts)

        self.logger.info(
            "[context_budget_emit] mode=learn ratio=%.3f%s",
            float(payload.get("context_pressure_ratio", 0.0) or 0.0),
            self._trace_tag(),
        )
        yield self.event_bus.context_budget(payload)
        self._init_tool_runtime_context(
            course_name=course_name,
            session_state=session_state,
            retrieval_query=retrieval_query,
            memory_query=memory_query,
            question_raw=question_raw,
            permission_mode=str(getattr(plan, "permission_mode", "standard") or "standard"),
            taskgraph_step=self._runtime_route_override(state) or "run_tutor",
        )

        yield self.event_bus.status("正在生成最终回答...")
        yield from self.tutor.teach_stream(
            user_message, course_name, context,
            context_sections=context_sections,
            retrieval_empty=retrieval_empty,
            allowed_tools=plan.allowed_tools,
            history=history
        )
        session_state = self._update_session_state(
            session_state,
            task_full_text=question_raw,
            task_summary=self._summarize_task_text(question_raw),
            current_stage="learn_completed",
            current_step_index=3,
            selected_memory=context_sections.get("memory_context", ""),
            history_summary_state=history_summary_state,
            tool_audit_refs=self._extract_tool_audit_refs(),
        )
        self._runtime_update_state_ref(state, session_state)
        if not runtime_managed:
            self._persist_session_state(session_state)
            yield self.event_bus.tool_calls(
                self._final_internal_tool_calls(
                    session_state=session_state,
                    history_summary_state=history_summary_state,
                )
            )

        if self._should_persist_learn_episode(user_message):
            doc_ids = [c["doc_id"] for c in citations_dicts] if citations_dicts else []
            if runtime_managed:
                effects = self._runtime_effects(state)
                effects["persist_memory"] = {
                    "kind": "learn_episode",
                    "course_name": course_name,
                    "question_raw": question_raw,
                    "doc_ids": doc_ids,
                }
            else:
                # 流式输出完成后仅在用户显式要求“记住”时写入情景记忆。
                try:
                    self.memory_service.save_learn_episode(course_name, question_raw, doc_ids)
                except Exception as _mem_err:
                    self.logger.warning("[memory] learn_qa_save_failed err=%s%s", str(_mem_err), self._trace_tag())

    def run_stream(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None
    ):
        """主流式入口，由 Runtime 编译 TaskGraph 并执行。"""
        self._tool_dedup_cache = {}
        yield from self.runtime.execute_stream(
            course_name=course_name,
            mode_hint=mode,
            user_message=user_message,
            state=state,
            history=history,
        )
