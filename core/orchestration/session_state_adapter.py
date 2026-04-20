"""Compatibility adapter for restoring SessionStateV1 from legacy payloads."""

from __future__ import annotations

import uuid
from typing import Any, Callable, Dict, List, Optional

from backend.schemas import SessionStateV1


class SessionStateAdapter:
    """Isolate legacy session-state recovery from runner orchestration logic."""

    @staticmethod
    def restore_from_inputs(
        *,
        history: Optional[List[Dict[str, Any]]],
        course_name: str,
        mode_hint: str,
        user_message: str,
        state: Optional[Dict[str, Any]],
        empty_history_summary_state: Callable[[], Dict[str, Any]],
        normalize_history_summary_state: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
        extract_internal_meta: Callable[[List[Dict[str, Any]], str], Any],
        extract_quiz_from_history: Callable[[List[Dict[str, Any]]], str],
        extract_exam_from_history: Callable[[List[Dict[str, Any]]], str],
        summarize_task_text: Callable[[str], str],
    ) -> SessionStateV1:
        history = history or []
        state = state or {}

        payload = extract_internal_meta(history, "session_state")
        if isinstance(payload, dict):
            try:
                restored = SessionStateV1.model_validate(payload)
                forced_session_id = str(state.get("session_id", "") or "").strip()
                if forced_session_id and restored.session_id != forced_session_id:
                    restored = restored.model_copy(update={"session_id": forced_session_id})
                return restored.model_copy(
                    update={
                        "course_name": course_name,
                        "requested_mode_hint": mode_hint,
                        "task_full_text": user_message or restored.task_full_text,
                        "task_summary": summarize_task_text(user_message or restored.task_full_text),
                    }
                )
            except Exception:
                pass

        legacy_history_summary_state = normalize_history_summary_state(extract_internal_meta(history, "history_summary_state"))
        if not legacy_history_summary_state:
            legacy_history_summary_state = empty_history_summary_state()
        legacy_last_quiz = extract_internal_meta(history, "quiz_meta")
        legacy_last_exam = extract_internal_meta(history, "exam_meta")

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
            inferred_practice = extract_quiz_from_history(history)
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
            inferred_exam = extract_exam_from_history(history)
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
            session_id=str(state.get("session_id", "") or "").strip() or uuid.uuid4().hex,
            course_name=course_name,
            requested_mode_hint=mode_hint,  # type: ignore[arg-type]
            resolved_mode=mode_hint,  # type: ignore[arg-type]
            task_full_text=user_message,
            task_summary=summarize_task_text(user_message),
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
