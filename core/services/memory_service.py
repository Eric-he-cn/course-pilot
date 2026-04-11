"""Memory-related retrieval and persistence extracted from runner/agents."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from backend.schemas import PracticeGradeSignal
from mcp_tools.client import MCPTools


class MemoryService:
    """Handles profile access, prefetch, and learn/practice/exam memory writes."""

    def __init__(self):
        self._request_cache: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def get_profile_context(course_name: str | None) -> str:
        if not course_name:
            return ""
        try:
            from memory.manager import get_memory_manager

            return str(get_memory_manager().get_profile_context(course_name) or "")
        except Exception:
            return ""

    @staticmethod
    def _build_memory_ctx(mem_result: Dict[str, Any], *, phase: str = "") -> str:
        if not isinstance(mem_result, dict) or not mem_result.get("success") or not mem_result.get("results"):
            return ""
        top_k = int(os.getenv("CB_MEMORY_TOPK", "2"))
        item_max_chars = int(os.getenv("CB_MEMORY_ITEM_MAX_CHARS", "100"))
        snippets: List[str] = []
        for result in mem_result.get("results", [])[: max(1, top_k)]:
            if isinstance(result, dict):
                text = result.get("content") or result.get("summary") or result.get("text") or ""
            else:
                text = str(result or "")
            text = str(text).strip()
            if text:
                snippets.append(text[: max(20, item_max_chars)])
        if not snippets:
            return ""
        title = "【该知识点历史错题参考】"
        if phase == "grade":
            title = "【该知识点历史错题参考（评分时请特别关注相同薄弱点）】"
        elif phase == "generate":
            title = "【该知识点历史错题参考（出题时请优先覆盖薄弱点）】"
        return f"\n\n{title}\n" + "\n".join(f"- {item}" for item in snippets)

    def prefetch_history_ctx(
        self,
        *,
        query: str,
        course_name: str,
        mode: str = "",
        agent: str = "",
        phase: str = "",
    ) -> str:
        try:
            top_k = int(os.getenv("CB_MEMORY_TOPK", "2"))
            key_payload = {
                "tool": "memory_search",
                "query": query,
                "course_name": course_name,
                "event_types": ["mistake", "practice", "exam", "qa_summary"],
                "mode": mode or None,
                "agent": agent or None,
                "phase": phase or None,
                "top_k": top_k,
            }
            cache_key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
            if cache_key in self._request_cache:
                mem = self._request_cache[cache_key]
            else:
                mem = MCPTools.call_tool(
                    "memory_search",
                    query=query,
                    course_name=course_name,
                    event_types=["mistake", "practice", "exam", "qa_summary"],
                    mode=mode or None,
                    agent=agent or None,
                    phase=phase or None,
                    top_k=top_k,
                )
                if isinstance(mem, dict):
                    self._request_cache[cache_key] = dict(mem)
            return self._build_memory_ctx(mem if isinstance(mem, dict) else {}, phase=phase)
        except Exception:
            return ""

    @staticmethod
    def save_learn_episode(course_name: str, question_raw: str, doc_ids: List[str]) -> None:
        try:
            from memory.manager import get_memory_manager

            content = f"用户要求记住: {question_raw}"
            if doc_ids:
                content += f"\n参考来源: {', '.join(dict.fromkeys(doc_ids))}"
            get_memory_manager().record_event(
                course_name=course_name,
                event_type="qa",
                content=content,
                importance=0.6,
                metadata={"doc_ids": doc_ids, "explicit_memory_request": True},
                increment_qa=True,
                mode="learn",
                agent="tutor",
                phase="answer",
            )
        except Exception:
            return

    @staticmethod
    def save_practice_grade(course_name: str, user_answer: str, history: list, response_text: str) -> None:
        try:
            from memory.manager import get_memory_manager

            question_summary = "（未能提取题目）"
            for msg in reversed(history[-20:]):
                if msg.get("role") == "assistant":
                    question_summary = msg.get("content", "")[:300]
                    break
            signal = PracticeGradeSignal.from_text(
                response_text=response_text,
                student_answer=user_answer,
                question_summary=question_summary,
            )
            content = (
                f"题目: {signal.question_summary}\n"
                f"学生答案: {signal.student_answer}\n"
                f"得分: {signal.score:.0f}"
            )
            if signal.mistake_tags:
                content += f"\n错误类型: {', '.join(signal.mistake_tags)}"
            get_memory_manager().record_event(
                course_name=course_name,
                event_type="mistake" if signal.is_mistake else "practice",
                content=content,
                importance=0.9 if signal.is_mistake else 0.4,
                metadata={"score": signal.score, "tags": signal.mistake_tags},
                score=signal.score,
                concepts=signal.mistake_tags,
                update_weak_points=signal.is_mistake,
                increment_practice=True,
                mode="practice",
                agent="grader",
                phase="grade",
            )
        except Exception:
            return

    @staticmethod
    def save_exam_grade(course_name: str, response_text: str) -> None:
        try:
            from memory.manager import get_memory_manager

            score = None
            score_patterns = [
                r"(?:总得分|总分)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*/\s*100",
                r"(?:总得分|总分)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*分",
            ]
            for pattern in score_patterns:
                match = re.search(pattern, response_text)
                if match:
                    score = float(match.group(1))
                    break
            weak_points: List[str] = []
            block = re.search(r"薄弱知识点[：:\s]*([\s\S]{0,300})(?:\n## |\n---|\Z)", response_text)
            if block:
                section = block.group(1)
                bullet_items = re.findall(r"(?:^|\n)\s*[-*•]\s*([^\n]{1,40})", section)
                if bullet_items:
                    weak_points = [x.strip() for x in bullet_items if x.strip()]
                else:
                    inline = re.sub(r"[\r\n]+", " ", section).strip()
                    weak_points = [x.strip() for x in re.split(r"[,，、；;]", inline) if x.strip()]
            weak_points = weak_points[:8]
            excerpt = re.sub(r"\n{3,}", "\n\n", response_text.strip().replace("\r", ""))[:900]
            content = "考试批改摘要：\n" + excerpt
            if score is not None:
                content = f"考试总分: {score:.0f}/100\n" + content
            if weak_points:
                content += f"\n薄弱知识点: {', '.join(weak_points)}"
            get_memory_manager().record_event(
                course_name=course_name,
                event_type="exam",
                content=content,
                importance=0.9 if (score is not None and score < 60) else 0.6,
                metadata={"score": score, "weak_points": weak_points},
                score=score,
                concepts=weak_points,
                update_weak_points=bool(weak_points),
                mode="exam",
                agent="grader",
                phase="grade",
            )
        except Exception:
            return
