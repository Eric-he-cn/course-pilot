from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.bootstrap import Application
from app.http.deps import current_workspace
from modules.agent.skills import (
    BUNDLE_MAX_BYTES, IMPORTABLE_TOOLS, SOURCE_MAX_BYTES, merge_bundle, read_zip,
)


def build_skills_router() -> APIRouter:
    """Skill 目录与导入：导入的 skill 默认关闭，权限按白名单收窄。"""
    router = APIRouter(prefix="/api/v2", tags=["skills"])

    def fail(status: int, code: str, message: str) -> HTTPException:
        return HTTPException(status_code=status, detail={"error": {"code": code, "message": message, "retryable": False}})

    @router.get("/skills")
    def list_skills(application: Application = Depends(current_workspace)) -> dict[str, object]:
        return {"skills": application.skills.catalog(), "importable_tools": list(IMPORTABLE_TOOLS)}

    @router.post("/skills", status_code=201)
    async def import_skill(
        file: list[UploadFile] = File(...), application: Application = Depends(current_workspace),
    ) -> dict[str, object]:
        """收单个 SKILL.md、一个 ZIP，或整个目录（前端按 file 字段重复上传，文件名带相对路径）。"""
        members, total = [], 0
        for item in file:
            raw = await item.read()
            total += len(raw)
            if total > BUNDLE_MAX_BYTES:
                raise fail(413, "payload_too_large", f"一次导入超过 {BUNDLE_MAX_BYTES // 1024 // 1024} MiB")
            members.append((item.filename or "", raw))
        try:
            if len(members) == 1 and members[0][0].lower().endswith(".zip"):
                members = read_zip(members[0][1])
            text, skipped = merge_bundle(members)
        except ValueError as error:
            raise fail(422, "invalid_skill", str(error)) from None
        if len(text.encode("utf-8")) > SOURCE_MAX_BYTES:
            raise fail(413, "payload_too_large", f"规程与附带资料合起来超过 {SOURCE_MAX_BYTES // 1024} KiB")
        try:
            definition = application.skills.import_skill(text)
        except ValueError as error:
            raise fail(422, "invalid_skill", str(error)) from None
        return {
            "name": definition.name, "status": definition.status, "origin": "user",
            "allowed_tools": list(definition.allowed_tools), "denied_tools": list(definition.denied_tools),
            "content_hash": definition.content_hash, "skipped_files": list(skipped),
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
