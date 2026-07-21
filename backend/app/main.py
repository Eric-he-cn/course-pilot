from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.bootstrap import build_application
from core.settings import Settings


def create_app(*, settings: Settings | None = None) -> FastAPI:
    from app.http.core import create_core_router
    from app.http.errors import error_response, normalize_http_detail
    from app.http.knowledge import build_knowledge_router
    from app.http.study import build_study_router
    application = build_application(settings or Settings.from_environment())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        application.sessions.recover_stale_turns()
        application.knowledge_jobs.start()
        try:
            yield
        finally:
            application.knowledge_jobs.shutdown()
            application.llm.close()

    app = FastAPI(title="CoursePilot 2.0 Demo", version="2.0.0", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, error: HTTPException) -> JSONResponse:
        return normalize_http_detail(error.status_code, error.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(status_code=422, code="invalid_request", message="请求参数无效", retryable=False)

    app.state.application = application
    app.include_router(create_core_router(application))
    app.include_router(
        build_knowledge_router(
            knowledge=application.knowledge,
            jobs=application.knowledge_jobs,
            courses=application.courses,
            llm_health=application.llm_health,
        )
    )
    app.include_router(build_study_router(learning=application.learning, planning=application.planning, courses=application.courses))
    return app


app = create_app()
