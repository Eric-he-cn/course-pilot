from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def error_response(*, status_code: int, code: str, message: str, retryable: bool = False) -> JSONResponse:
    """Stable API error envelope, with ``detail`` retained for the Web client."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "retryable": retryable}, "detail": message},
    )


def normalize_http_detail(status_code: int, detail: Any) -> JSONResponse:
    if isinstance(detail, dict):
        nested = detail.get("error") if isinstance(detail.get("error"), dict) else detail
        code = str(nested.get("code", "http_error"))
        message = str(nested.get("message", "请求失败"))
        retryable = bool(nested.get("retryable", status_code >= 500))
    else:
        code = "not_found" if status_code == 404 else "http_error"
        message = str(detail)
        retryable = status_code >= 500
    return error_response(status_code=status_code, code=code, message=message, retryable=retryable)
