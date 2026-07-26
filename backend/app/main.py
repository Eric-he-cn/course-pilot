from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.workspaces import Workspaces
from core.settings import Settings


def create_app(*, settings: Settings | None = None) -> FastAPI:
    from app.http.core import create_core_router
    from app.http.errors import error_response, normalize_http_detail
    from app.http.knowledge import build_knowledge_router
    from app.http.skills import build_skills_router
    from app.http.study import build_study_router

    resolved = settings or Settings.from_environment()
    workspaces = Workspaces(resolved)
    if workspaces.legacy_data_pending():
        # 只提示不自动搬：数据是用户的，什么时候迁移由他定。
        # 这条状态也经 /health 送到界面上——光靠控制台没人看得见。
        print("[workspace] data/ 根下仍是旧布局的数据；运行 "
              ".venv/bin/python scripts/migrate_to_users.py 可迁进按用户隔离的目录")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 失活恢复与 job worker 启动改成随工作区懒建（见 Workspaces.for_id）：
        # 进程启动时还不知道有哪些用户。
        try:
            yield
        finally:
            workspaces.close_all()

    app = FastAPI(title="CoursePilot 2.0 Demo", version="2.0.0", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, error: HTTPException) -> JSONResponse:
        return normalize_http_detail(error.status_code, error.detail)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(status_code=422, code="invalid_request", message="请求参数无效", retryable=False)

    app.state.workspaces = workspaces
    app.include_router(create_core_router())
    app.include_router(build_knowledge_router(legacy_data_pending=workspaces.legacy_data_pending))
    app.include_router(build_skills_router())
    app.include_router(build_study_router())
    return app


app = create_app()
