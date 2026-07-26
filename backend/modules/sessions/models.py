from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal
ScopeMode = Literal["general", "course"]
ResolutionStatus = Literal["resolved", "ambiguous", "unresolved"]
@dataclass(frozen=True)
class SessionSummary:
    id: str; title: str; scope_mode: ScopeMode; course_id: str | None; resolved_course_id: str | None; course_name: str | None; course_color: str | None; source: str; updated_at: str
@dataclass(frozen=True)
class Message:
    id: str; turn_id: str | None; role: str; content: str; citations: list[dict]; status: str; created_at: str
    resolution_status: ResolutionStatus | None = None
    resolved_course_id: str | None = None
    resolved_course_name: str | None = None
    resolved_course_color: str | None = None
    resolution_reason: str | None = None
    activity: list[dict] = field(default_factory=list)
@dataclass(frozen=True)
class Attachment:
    id: str; session_id: str; filename: str; mime_type: str; byte_size: int; width: int; height: int
    transcription: str; needs_confirmation: bool; provider: str; model: str; created_at: str
@dataclass(frozen=True)
class Turn:
    id: str; session_id: str; status: str; client_request_id: str; created_at: str
@dataclass(frozen=True)
class ResolvedCourseContext:
    turn_id: str; status: ResolutionStatus; course_id: str | None; course_name: str | None; course_color: str | None; reason: str; resolver_version: str = "course_resolver_v1"
    # 歧义时问题命中的课程名，用于让澄清话术给出可选项
    candidates: tuple[str, ...] = ()
    # 学科分类器的执行情况，只进 trace，不落库也不进 SSE
    classifier: dict | None = None
