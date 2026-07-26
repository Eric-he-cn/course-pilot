"""按请求解析当前用户的工作区。"""
from __future__ import annotations

from urllib.parse import unquote

from fastapi import HTTPException, Request

from app.bootstrap import Application
from core.identity import InvalidUsername

USER_HEADER = "X-CoursePilot-User"


def current_workspace(request: Request) -> Application:
    """写成普通 def 而不是带清理的 yield 依赖：SSE 生成器在请求作用域结束之后
    才被消费，yield 依赖的清理会在流还没跑完时就执行。"""
    workspaces = request.app.state.workspaces
    raw = request.headers.get(USER_HEADER, "")
    if not raw:
        return workspaces.default()
    try:
        # 头值按 ByteString 传输，中日韩用户名前端会 encodeURIComponent 后再放进来。
        return workspaces.for_username(unquote(raw))
    except InvalidUsername as error:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "invalid_username", "message": str(error), "retryable": False}},
        ) from error
