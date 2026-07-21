from __future__ import annotations
import json
import sqlite3
from core.common import new_id, utc_now
from modules.courses.api import CourseCatalogPort
from .api import CourseResolverPort, SessionBusyError
from .models import Message, ResolvedCourseContext, SessionSummary, Turn
from .repository import SessionRepository
class SessionService:
    def __init__(self, repository: SessionRepository, courses: CourseCatalogPort, resolver: CourseResolverPort) -> None: self._repository, self._courses, self._resolver = repository, courses, resolver
    def _summary(self, row) -> SessionSummary:
        projected_id = row["course_id"] or row["last_resolved_course_id"]; course = self._courses.get_course(projected_id) if projected_id else None
        return SessionSummary(row["id"], row["title"], row["scope_mode"], row["course_id"], row["last_resolved_course_id"], course.name if course else None, course.color if course else None, row["source"], row["updated_at"])
    def list_sessions(self, *, scope_mode: str | None = None, course_id: str | None = None) -> list[SessionSummary]: return [self._summary(row) for row in self._repository.list_session_rows(scope_mode=scope_mode, course_id=course_id)]
    def get_session(self, session_id: str) -> SessionSummary | None:
        row = self._repository.get_session_row(session_id); return self._summary(row) if row else None
    def create_session(self, *, scope_mode: str, course_id: str | None, title: str | None, source: str, owner_id: str = "local-web") -> SessionSummary:
        if scope_mode not in {"general", "course"}: raise ValueError("scope_mode 必须是 general 或 course")
        if scope_mode == "course" and not course_id: raise ValueError("课程会话必须指定 course_id")
        if scope_mode == "general" and course_id is not None: raise ValueError("通用会话不能指定 course_id")
        if course_id and not self._courses.get_course(course_id): raise LookupError("课程不存在")
        if source not in {"web", "feishu"}: raise ValueError("不支持的会话来源")
        if source == "feishu":
            if scope_mode != "general": raise ValueError("飞书首版只能创建通用会话")
            if not owner_id.strip(): raise ValueError("飞书会话必须绑定 owner")
            existing = self._repository.get_source_session_row(source="feishu", scope_mode="general", owner_id=owner_id)
            if existing: return self._summary(existing)
        session_id, timestamp = new_id("session"), utc_now()
        try:
            self._repository.insert_session(session_id=session_id, title=(title or "新学习对话").strip()[:120] or "新学习对话", scope_mode=scope_mode, course_id=course_id, source=source, owner_id=owner_id, timestamp=timestamp)
        except sqlite3.IntegrityError:
            # The Feishu uniqueness index resolves a concurrent first delivery.
            existing = self._repository.get_source_session_row(source="feishu", scope_mode="general", owner_id=owner_id)
            if existing: return self._summary(existing)
            raise
        return self.get_session(session_id)  # type: ignore[return-value]
    def list_messages(self, session_id: str) -> list[Message]:
        if not self.get_session(session_id): raise LookupError("会话不存在")
        messages = []
        for row in self._repository.list_message_rows(session_id):
            resolved_course = self._courses.get_course(row["resolved_course_id"]) if row["resolved_course_id"] else None
            messages.append(Message(row["id"], row["turn_id"], row["role"], row["content"], json.loads(row["citations_json"]), row["status"], row["created_at"], row["resolution_status"], row["resolved_course_id"], resolved_course.name if resolved_course else None, resolved_course.color if resolved_course else None, row["resolution_reason"]))
        return messages
    def start_turn(self, *, session_id: str, client_request_id: str) -> tuple[Turn, bool]:
        if not self.get_session(session_id): raise LookupError("会话不存在")
        if not client_request_id.strip(): raise ValueError("client_request_id 不能为空")
        try:
            row, created = self._repository.get_or_create_turn(turn_id=new_id("turn"), session_id=session_id, client_request_id=client_request_id, timestamp=utc_now())
        except sqlite3.IntegrityError as error:
            raise SessionBusyError("该会话正在生成回答") from error
        return Turn(row["id"], row["session_id"], row["status"], row["client_request_id"], row["created_at"]), created
    def resolve_turn(self, *, turn: Turn, message: str) -> ResolvedCourseContext:
        session = self.get_session(turn.session_id)
        if not session: raise LookupError("会话不存在")
        context = self._resolver.resolve(turn_id=turn.id, session=session, message=message); self._repository.save_course_context(turn_id=turn.id, resolution_status=context.status, resolved_course_id=context.course_id, resolver_version=context.resolver_version, reason=context.reason, timestamp=utc_now()); self._repository.update_last_resolved_course(session_id=turn.session_id, course_id=context.course_id); return context
    def append_message(self, *, session_id: str, turn_id: str | None, role: str, content: str, citations: list[dict] | None = None, status: str = "complete") -> Message:
        message_id, timestamp = new_id("message"), utc_now(); safe = citations or []; self._repository.insert_message(message_id=message_id, session_id=session_id, turn_id=turn_id, role=role, content=content, citations=safe, status=status, timestamp=timestamp); return Message(message_id, turn_id, role, content, safe, status, timestamp)
    def complete_turn(self, turn_id: str, *, status: str) -> None: self._repository.finish_turn(turn_id=turn_id, status=status, timestamp=utc_now())
