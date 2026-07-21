from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from modules.courses.api import CourseCatalogPort
from modules.learning.service import LearningService
from modules.planning.service import PlanningService


def build_study_router(*, learning: LearningService, planning: PlanningService, courses: CourseCatalogPort) -> APIRouter:
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

    return router
