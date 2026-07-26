"""按请求解析当前用户的工作区。"""
from __future__ import annotations

from urllib.parse import unquote

from fastapi import HTTPException, Request

from app.bootstrap import Application
from core.identity import InvalidUsername

USER_HEADER = "X-CoursePilot-User"
MODEL_HEADER = "X-CoursePilot-Model"
THINKING_HEADER = "X-CoursePilot-Thinking"


def model_choice(request: Request) -> tuple[str | None, str | None]:
    """本轮用哪个模型、思考档位。都不带就用配置里的第一个模型与它的默认档位。
    选择放在请求头而不是服务端：多个标签页可以各用各的，服务端保持无状态。
    档位名不在这里校验，交给 bootstrap 的选择函数——认不出就落回默认。"""
    key = request.headers.get(MODEL_HEADER, "").strip() or None
    tier = request.headers.get(THINKING_HEADER, "").strip().lower() or None
    return key, tier


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
