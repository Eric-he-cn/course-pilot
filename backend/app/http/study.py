from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from modules.courses.api import CourseCatalogPort
from modules.learning.service import LearningService
from modules.memory.store import MemoryStore
from modules.notes.store import NoteStore
from modules.planning.service import PlanningService


def build_study_router(*, learning: LearningService, planning: PlanningService, courses: CourseCatalogPort, notes: NoteStore, memory: MemoryStore) -> APIRouter:
    """学习计划与学习档案的只读骨架接口；写链路随对应功能落地。"""
    router = APIRouter(prefix="/api/v2", tags=["study"])

    def require_course(course_id: str) -> None:
        if courses.get_course(course_id) is None:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "课程不存在", "retryable": False}})

    @router.get("/courses/{course_id}/plan")
    def get_plan(course_id: str) -> dict[str, object]:
        require_course(course_id)
        plan = planning.get_plan(course_id=course_id)
        return {"plan": asdict(plan) if plan else None}

    @router.get("/courses/{course_id}/archive")
    def get_archive(course_id: str) -> dict[str, object]:
        require_course(course_id)
        return asdict(learning.get_archive(course_id=course_id))

    @router.get("/memory")
    def read_user_memory() -> dict[str, object]:
        return {"scope": "user", "content": memory.read_user()}

    @router.put("/memory")
    def write_user_memory(payload: dict) -> dict[str, object]:
        try:
            memory.write_whole(scope="user", content=str(payload.get("content") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        return {"scope": "user", "content": memory.read_user()}

    @router.get("/courses/{course_id}/memory")
    def read_course_memory(course_id: str) -> dict[str, object]:
        require_course(course_id)
        return {"scope": "course", "course_id": course_id, "content": memory.read_course(course_id)}

    @router.put("/courses/{course_id}/memory")
    def write_course_memory(course_id: str, payload: dict) -> dict[str, object]:
        require_course(course_id)
        try:
            memory.write_whole(scope="course", content=str(payload.get("content") or ""), course_id=course_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        return {"scope": "course", "course_id": course_id, "content": memory.read_course(course_id)}

    @router.get("/courses/{course_id}/notes")
    def list_notes(course_id: str) -> dict[str, object]:
        require_course(course_id)
        return {"notes": [asdict(note) for note in notes.list_notes(course_id=course_id)]}

    @router.get("/courses/{course_id}/notes/{title}")
    def read_note(course_id: str, title: str) -> dict[str, object]:
        require_course(course_id)
        try:
            return {"title": title, "content": notes.read(course_id=course_id, title=title)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": str(exc), "retryable": False}}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc

    return router
