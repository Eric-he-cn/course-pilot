from __future__ import annotations

import json
from dataclasses import asdict
from functools import partial
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.bootstrap import Application
from app.http.deps import current_workspace, model_choice
from contracts.llm import LLMProviderError
from modules.sessions.api import VisionFeatureDisabledError


class CourseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: Optional[str] = None


class CourseUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    wiki_enabled: Optional[bool] = None


class SessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_mode: str
    course_id: Optional[str] = None
    title: Optional[str] = None


class SessionRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    client_request_id: str = Field(default_factory=lambda: str(uuid4()))
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)


def _not_found(error: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail={"error": {"code": "not_found", "message": str(error), "retryable": False}})


def create_core_router() -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["core"])

    @router.get("/courses")
    def list_courses(application: Application = Depends(current_workspace)):
        return [asdict(course) for course in application.courses.list_courses()]

    @router.post("/courses", status_code=201)
    def create_course(request: CourseCreateRequest, application: Application = Depends(current_workspace)):
        try:
            return asdict(application.courses.create_course(name=request.name, color=request.color))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc

    @router.patch("/courses/{course_id}")
    def update_course(course_id: str, request: CourseUpdateRequest, application: Application = Depends(current_workspace)):
        try:
            course = application.courses.update_course(course_id, name=request.name, wiki_enabled=request.wiki_enabled)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        if not course:
            raise _not_found(LookupError("课程不存在"))
        return asdict(course)

    @router.delete("/courses/{course_id}", status_code=204)
    def delete_course(course_id: str, application: Application = Depends(current_workspace)) -> None:
        try:
            application.courses.delete_course(course_id)
        except LookupError as exc:
            raise _not_found(exc) from exc

    @router.get("/sessions")
    def list_sessions(scope_mode: Optional[str] = None, course_id: Optional[str] = None, application: Application = Depends(current_workspace)):
        return [asdict(session) for session in application.sessions.list_sessions(scope_mode=scope_mode, course_id=course_id)]

    @router.post("/sessions", status_code=201)
    def create_session(request: SessionCreateRequest, application: Application = Depends(current_workspace)):
        try:
            # HTTP 固定是 Web 渠道；其他渠道由适配器直接调服务，浏览器伪造不出别的来源。
            return asdict(application.sessions.create_session(scope_mode=request.scope_mode, course_id=request.course_id, title=request.title, source="web"))
        except LookupError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc

    @router.patch("/sessions/{session_id}")
    def rename_session(session_id: str, request: SessionRenameRequest, application: Application = Depends(current_workspace)):
        try:
            return asdict(application.sessions.rename_session(session_id=session_id, title=request.title))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        except LookupError as exc:
            raise _not_found(exc) from exc

    @router.delete("/sessions/{session_id}", status_code=204)
    def delete_session(session_id: str, application: Application = Depends(current_workspace)) -> None:
        try:
            application.sessions.delete_session(session_id)
        except LookupError as exc:
            raise _not_found(exc) from exc

    @router.get("/sessions/{session_id}/messages")
    def list_messages(session_id: str, application: Application = Depends(current_workspace)):
        try:
            session = application.sessions.get_session(session_id)
            messages = application.sessions.list_messages(session_id)
        except LookupError as exc:
            raise _not_found(exc) from exc
        # role='tool' 是落库的工具正文，界面不画它：那是给模型跨轮读回的资料，
        # 当成对话气泡贴出来只会把检索原文摊满整个会话。
        return {"session": asdict(session) if session else None,
                "messages": [asdict(message) for message in messages if message.role != "tool"]}

    @router.post("/sessions/{session_id}/attachments", status_code=201)
    async def upload_attachment(session_id: str, file: UploadFile = File(...), application: Application = Depends(current_workspace)):
        try:
            content = await file.read()
            attachment = await run_in_threadpool(
                partial(
                    application.sessions.create_attachment,
                    session_id=session_id,
                    filename=file.filename or "image",
                    mime_type=file.content_type or "application/octet-stream",
                    content=content,
                )
            )
            return asdict(attachment)
        except VisionFeatureDisabledError as exc:
            raise HTTPException(status_code=409, detail={"code": "feature_disabled", "message": str(exc)}) from exc
        except LookupError as exc:
            raise _not_found(exc) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"error": {"code": "invalid_request", "message": str(exc), "retryable": False}}) from exc
        except LLMProviderError as exc:
            raise HTTPException(status_code=502, detail={"error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}}) from exc
        finally:
            await file.close()

    @router.post("/sessions/{session_id}/turns")
    def turn(session_id: str, request: TurnRequest, http_request: Request, application: Application = Depends(current_workspace)):
        if application.sessions.get_session(session_id) is None:
            raise _not_found(LookupError("会话不存在"))
        model_key, thinking = model_choice(http_request)
        def stream():
            for payload in application.turns.run(session_id=session_id, message=request.message, client_request_id=request.client_request_id, attachment_ids=request.attachment_ids, model_key=model_key, thinking=thinking):
                yield f"event: {payload['event']}\ndata: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return router
