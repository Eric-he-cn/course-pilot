from __future__ import annotations

from dataclasses import asdict
from functools import partial
from typing import Callable

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from modules.courses.api import CourseCatalogPort
from modules.knowledge.api import KnowledgeFeatureDisabledError, MaterialNotIndexedError
from app.bootstrap import Application
from app.http.deps import current_workspace
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=6, ge=1, le=20)


def _not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def _material_payload(material: object) -> dict[str, object]:
    result = asdict(material)
    # Keep the storage DTO while also supplying the frontend's display aliases.
    result["content_type"] = result["mime_type"]
    result["size_bytes"] = result["byte_size"]
    result["status"] = result["index_status"]
    return result


def _job_payload(job: object) -> dict[str, object]:
    result = asdict(job)
    result["error"] = result["error_message"]
    return result


def build_knowledge_router(*, legacy_data_pending: Callable[[], bool] = lambda: False) -> APIRouter:
    """Build the course-scoped knowledge HTTP adapter.

    The course check is performed through the public CourseCatalogPort.  The
    knowledge repository never queries the courses table or other modules.
    """
    router = APIRouter(prefix="/api/v2", tags=["knowledge"])

    def require_course(application: Application, course_id: str) -> None:
        if application.courses.get_course(course_id) is None:
            raise _not_found("课程不存在")

    @router.get("/courses/{course_id}/materials")
    def list_materials(course_id: str, application: Application = Depends(current_workspace)) -> list[dict[str, object]]:
        require_course(application, course_id)
        return [_material_payload(material) for material in application.knowledge.list_materials(course_id=course_id)]

    @router.post("/courses/{course_id}/materials", status_code=201)
    async def upload_material(course_id: str, file: UploadFile = File(...), application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        try:
            content = await file.read()
            material = await run_in_threadpool(
                partial(
                    application.knowledge.upload_material,
                    course_id=course_id,
                    filename=file.filename or "upload",
                    mime_type=file.content_type or "application/octet-stream",
                    content=content,
                )
            )
            return _material_payload(material)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        finally:
            await file.close()

    @router.delete("/materials/{material_id}", status_code=204)
    def delete_material(material_id: str, application: Application = Depends(current_workspace)) -> None:
        # 删除会跨越 knowledge 与 learning 的表，编排放在 courses 服务里统一排顺序。
        try:
            application.courses.delete_material(material_id)
        except LookupError as error:
            raise _not_found(str(error)) from error

    @router.post("/materials/{material_id}/index")
    def index_material(material_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        try:
            job = application.knowledge.enqueue_index(material_id=material_id)
            application.knowledge_jobs.submit(job.id)
            return _job_payload(application.knowledge.get_job(job_id=job.id) or job)
        except ValueError as error:
            raise _not_found(str(error)) from error

    @router.post("/materials/{material_id}/ocr/estimate")
    def estimate_ocr(material_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        """真 OCR 两页量成本再外推。本身要花一点额度，所以是 POST 而不是 GET。"""
        try:
            return application.knowledge.estimate_ocr(material_id=material_id)
        except KnowledgeFeatureDisabledError as error:
            raise HTTPException(status_code=409, detail={"code": "feature_disabled", "message": str(error)}) from error
        except ValueError as error:
            raise _not_found(str(error)) from error

    @router.post("/materials/{material_id}/ocr")
    def start_ocr(material_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        try:
            job = application.knowledge.approve_ocr(material_id=material_id)
            application.knowledge_jobs.submit(job.id)
            return _job_payload(application.knowledge.get_job(job_id=job.id) or job)
        except KnowledgeFeatureDisabledError as error:
            raise HTTPException(status_code=409, detail={"code": "feature_disabled", "message": str(error)}) from error
        except ValueError as error:
            raise _not_found(str(error)) from error

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        job = application.knowledge.get_job(job_id=job_id)
        if job is None:
            raise _not_found("任务不存在")
        return _job_payload(job)

    @router.post("/courses/{course_id}/knowledge/search")
    def search_course_knowledge(course_id: str, body: SearchRequest, application: Application = Depends(current_workspace)) -> list[dict[str, object]]:
        require_course(application, course_id)
        return [
            {
                "material_id": hit.citation.material_id, "material_name": hit.citation.document,
                "page": hit.citation.page, "chunk_id": hit.citation.chunk_id,
                "text": hit.citation.snippet, "score": hit.citation.score, "course_id": course_id,
            }
            for hit in application.knowledge.search_course(course_id=course_id, query=body.query, limit=body.limit)
        ]

    @router.post("/materials/{material_id}/wiki")
    def build_wiki(material_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        try:
            job = application.knowledge.enqueue_wiki_build(material_id=material_id)
            application.knowledge_jobs.submit(job.id)
            return _job_payload(application.knowledge.get_job(job_id=job.id) or job)
        except KnowledgeFeatureDisabledError as error:
            raise HTTPException(status_code=409, detail={"code": "feature_disabled", "message": str(error)}) from error
        except MaterialNotIndexedError as error:
            raise HTTPException(status_code=409, detail={"code": "material_not_indexed", "message": str(error)}) from error
        except ValueError as error:
            raise _not_found(str(error)) from error

    @router.get("/courses/{course_id}/wiki")
    def list_wiki_pages(course_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        return {"pages": application.knowledge.wiki_pages(course_id=course_id)}

    @router.get("/courses/{course_id}/wiki/{concept_id}")
    def read_wiki_page(course_id: str, concept_id: str, application: Application = Depends(current_workspace)) -> dict[str, object]:
        require_course(application, course_id)
        try:
            return {"concept_id": concept_id, "content": application.knowledge.wiki_page(course_id=course_id, concept_id=concept_id)}
        except (LookupError, ValueError) as error:
            raise _not_found(str(error)) from error

    @router.get("/health")
    def health(application: Application = Depends(current_workspace)) -> dict[str, object]:
        return {**application.knowledge.health(), "llm": application.llm_health(), "vision": application.vision_health(),
                "web": application.web_health(), "workspace": {"legacy_data_pending": legacy_data_pending()}}

    return router
