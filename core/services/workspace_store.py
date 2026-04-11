"""Workspace-backed persistence for session state and saved records."""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.schemas import GradeReport, Quiz, SessionStateV1


class WorkspaceStore:
    """Owns workspace-relative paths and file persistence side effects."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    @staticmethod
    def _safe_course_name(course_name: str) -> str:
        safe_name = os.path.basename(str(course_name or "").strip())
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError(f"无效的课程名称: {course_name!r}")
        return safe_name

    def get_workspace_path(self, course_name: str) -> str:
        return os.path.join(self.data_dir, self._safe_course_name(course_name))

    def _ensure_dir(self, *parts: str) -> str:
        path = os.path.join(*parts)
        os.makedirs(path, exist_ok=True)
        return path

    def load_session_state(self, course_name: str, session_id: str) -> Optional[SessionStateV1]:
        if not session_id:
            return None
        sessions_dir = self._ensure_dir(self.get_workspace_path(course_name), "sessions")
        path = os.path.join(sessions_dir, f"{session_id}.json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return SessionStateV1.model_validate(payload)

    def save_session_state(self, session_state: SessionStateV1) -> str:
        sessions_dir = self._ensure_dir(self.get_workspace_path(session_state.course_name), "sessions")
        path = os.path.join(sessions_dir, f"{session_state.session_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session_state.model_dump(), f, ensure_ascii=False, indent=2)
        return path

    def save_mistake(
        self,
        course_name: str,
        quiz: Quiz,
        student_answer: str,
        grade_report: GradeReport,
    ) -> str:
        mistakes_dir = self._ensure_dir(self.get_workspace_path(course_name), "mistakes")
        path = os.path.join(mistakes_dir, "mistakes.jsonl")
        payload = {
            "timestamp": datetime.now().isoformat(),
            "question": quiz.question,
            "student_answer": student_answer,
            "standard_answer": quiz.standard_answer,
            "score": grade_report.score,
            "feedback": grade_report.feedback,
            "mistake_tags": grade_report.mistake_tags,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path

    def save_practice_record(self, course_name: str, user_message: str, history: List[Dict[str, Any]], response_text: str) -> str:
        practices_dir = self._ensure_dir(self.get_workspace_path(course_name), "practices")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"练习记录_{timestamp}.md"
        path = os.path.join(practices_dir, filename)

        quiz_content = None
        for msg in reversed(history[-20:]):
            if msg.get("role") == "assistant":
                quiz_content = msg.get("content", "")
                break

        md = f"""# 练习记录

**时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**课程**：{course_name}

---

## 题目

{quiz_content or '（未能提取题目内容）'}

---

## 我的答案

{user_message}

---

## 评分与详细解析

{response_text}
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        return f"practices/{filename}"

    def save_exam_record(self, course_name: str, user_message: str, history: List[Dict[str, Any]], response_text: str) -> str:
        exams_dir = self._ensure_dir(self.get_workspace_path(course_name), "exams")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"考试记录_{timestamp}.md"
        path = os.path.join(exams_dir, filename)

        exam_paper = None
        for msg in reversed(history[-30:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if any(kw in content for kw in ["模拟考试试卷", "第一部分", "第二部分"]):
                    exam_paper = content
                    break

        md = f"""# 考试记录

**时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**课程**：{course_name}

---

## 试卷

{exam_paper or '（未能提取试卷内容）'}

---

## 我的答案

{user_message}

---

## 批改报告

{response_text}
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        return f"exams/{filename}"
