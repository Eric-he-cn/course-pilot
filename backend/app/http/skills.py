from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from app.bootstrap import Application
from app.http.deps import current_workspace
from modules.agent.skills import IMPORTABLE_TOOLS, SOURCE_MAX_BYTES


def build_skills_router() -> APIRouter:
    """Skill 目录与导入：导入的 skill 默认关闭，权限按白名单收窄。"""
    router = APIRouter(prefix="/api/v2", tags=["skills"])

    def fail(status: int, code: str, message: str) -> HTTPException:
        return HTTPException(status_code=status, detail={"error": {"code": code, "message": message, "retryable": False}})

    @router.get("/skills")
    def list_skills(application: Application = Depends(current_workspace)) -> dict[str, object]:
        return {"skills": application.skills.catalog(), "importable_tools": list(IMPORTABLE_TOOLS)}

    @router.post("/skills", status_code=201)
    async def import_skill(file: UploadFile, application: Application = Depends(current_workspace)) -> dict[str, object]:
        if not (file.filename or "").lower().endswith(".md"):
            raise fail(422, "unsupported_media_type", "只接受 .md 文件（首版仅支持 prompt-only skill）")
        raw = await file.read()
        if len(raw) > SOURCE_MAX_BYTES:
            raise fail(413, "payload_too_large", f"SKILL.md 超过 {SOURCE_MAX_BYTES // 1024} KiB")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise fail(422, "invalid_encoding", "文件不是 UTF-8 文本") from None
        try:
            definition = application.skills.import_skill(text)
        except ValueError as error:
            raise fail(422, "invalid_skill", str(error)) from None
        return {
            "name": definition.name, "status": definition.status, "origin": "user",
            "allowed_tools": list(definition.allowed_tools), "denied_tools": list(definition.denied_tools),
            "content_hash": definition.content_hash,
        }

    @router.patch("/skills/{name}")
    def set_enabled(name: str, payload: dict, application: Application = Depends(current_workspace)) -> dict[str, object]:
        try:
            definition = application.skills.set_enabled(name=name, enabled=bool(payload.get("enabled")))
        except LookupError:
            raise fail(404, "not_found", "该 skill 不存在或不是导入的 skill") from None
        except ValueError as error:
            raise fail(409, "permission_denied", str(error)) from None
        return {"name": definition.name, "status": definition.status}

    @router.delete("/skills/{name}", status_code=204)
    def remove(name: str, application: Application = Depends(current_workspace)) -> None:
        try:
            application.skills.remove(name=name)
        except LookupError:
            raise fail(404, "not_found", "该 skill 不存在或不是导入的 skill") from None

    return router
