from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException

from modules.courses.api import CourseCatalogPort
from modules.learning.service import LearningService
from modules.memory.store import MemoryStore
from modules.notes.store import NoteStore
from app.bootstrap import Application
from app.http.deps import current_workspace


def build_study_router() -> APIRouter:
    """学习计划与学习档案的只读骨架接口；写链路随对应功能落地。"""
    router = APIRouter(prefix="/api/v2", tags=["study"])

    def require_course(application: Application, course_id: str) -> None:
        if application.courses.get_course(course_id) is None:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": "课程不存在", "retryable": False}})

    @router.get("/courses/{course_id}/plan")
    def get_plan(course_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        plan = application.planning.get_plan(course_id=course_id)
        return {"plan": asdict(plan) if plan else None}

    @router.get("/courses/{course_id}/archive")
    def get_archive(course_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        return asdict(application.learning.get_archive(course_id=course_id))

    @router.get("/memory")
    def read_user_memory(application: Application = Depends(current_workspace)) -> dict[str, object]:
        return {"scope": "user", "content": application.memory.read_user()}

    @router.put("/memory")
    def write_user_memory(payload: dict, application: Application = Depends(current_workspace)) -> dict[str, object]:
        try:
            application.memory.write_whole(scope="user", content=str(payload.get("content") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        return {"scope": "user", "content": application.memory.read_user()}

    @router.get("/courses/{course_id}/memory")
    def read_course_memory(course_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        return {"scope": "course", "course_id": course_id, "content": application.memory.read_course(course_id)}

    @router.put("/courses/{course_id}/memory")
    def write_course_memory(course_id: str, payload: dict, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        try:
            application.memory.write_whole(scope="course", content=str(payload.get("content") or ""), course_id=course_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        return {"scope": "course", "course_id": course_id, "content": application.memory.read_course(course_id)}

    @router.get("/courses/{course_id}/notes")
    def list_notes(course_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        return {"notes": [asdict(note) for note in application.notes.list_notes(course_id=course_id)]}

    @router.get("/courses/{course_id}/notes/{title}")
    def read_note(course_id: str, title: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        try:
            return {"title": title, "content": application.notes.read(course_id=course_id, title=title)}
        except LookupError as exc:
            raise HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": str(exc), "retryable": False}}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc

    return router
