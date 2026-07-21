from __future__ import annotations

from dataclasses import asdict
from functools import partial
from typing import Callable

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from modules.courses.api import CourseCatalogPort
from modules.knowledge.api import KnowledgeFeatureDisabledError, MaterialNotIndexedError
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


def build_knowledge_router(
    *,
    knowledge: KnowledgeService,
    jobs: KnowledgeJobWorker,
    courses: CourseCatalogPort,
    llm_health: Callable[[], dict[str, object]],
    vision_health: Callable[[], dict[str, object]] = lambda: {},
) -> APIRouter:
    """Build the course-scoped knowledge HTTP adapter.

    The course check is performed through the public CourseCatalogPort.  The
    knowledge repository never queries the courses table or other modules.
    """
    router = APIRouter(prefix="/api/v2", tags=["knowledge"])

    def require_course(course_id: str) -> None:
        if courses.get_course(course_id) is None:
            raise _not_found("课程不存在")

    @router.get("/courses/{course_id}/materials")
    def list_materials(course_id: str) -> list[dict[str, object]]:
        require_course(course_id)
        return [_material_payload(material) for material in knowledge.list_materials(course_id=course_id)]

    @router.post("/courses/{course_id}/materials", status_code=201)
    async def upload_material(course_id: str, file: UploadFile = File(...)) -> dict[str, object]:
        require_course(course_id)
        try:
            content = await file.read()
            material = await run_in_threadpool(
                partial(
                    knowledge.upload_material,
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

    @router.post("/materials/{material_id}/index")
    def index_material(material_id: str) -> dict[str, object]:
        try:
            job = knowledge.enqueue_index(material_id=material_id)
            jobs.submit(job.id)
            return _job_payload(knowledge.get_job(job_id=job.id) or job)
        except ValueError as error:
            raise _not_found(str(error)) from error

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        job = knowledge.get_job(job_id=job_id)
        if job is None:
            raise _not_found("任务不存在")
        return _job_payload(job)

    @router.post("/courses/{course_id}/knowledge/search")
    def search_course_knowledge(course_id: str, body: SearchRequest) -> list[dict[str, object]]:
        require_course(course_id)
        return [
            {
                "material_id": hit.citation.material_id, "material_name": hit.citation.document,
                "page": hit.citation.page, "chunk_id": hit.citation.chunk_id,
                "text": hit.citation.snippet, "score": hit.citation.score, "course_id": course_id,
            }
            for hit in knowledge.search_course(course_id=course_id, query=body.query, limit=body.limit)
        ]

    @router.post("/materials/{material_id}/wiki")
    def build_wiki(material_id: str) -> dict[str, object]:
        try:
            job = knowledge.enqueue_wiki_build(material_id=material_id)
            jobs.submit(job.id)
            return _job_payload(knowledge.get_job(job_id=job.id) or job)
        except KnowledgeFeatureDisabledError as error:
            raise HTTPException(status_code=409, detail={"code": "feature_disabled", "message": str(error)}) from error
        except MaterialNotIndexedError as error:
            raise HTTPException(status_code=409, detail={"code": "material_not_indexed", "message": str(error)}) from error
        except ValueError as error:
            raise _not_found(str(error)) from error

    @router.get("/health")
    def health() -> dict[str, object]:
        return {**knowledge.health(), "llm": llm_health(), "vision": vision_health()}

    return router
