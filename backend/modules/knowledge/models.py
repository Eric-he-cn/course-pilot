from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Material:
    id: str
    course_id: str
    filename: str
    mime_type: str
    byte_size: int
    index_status: str
    created_at: str
    updated_at: str
    # 索引产物计数：embedded_count < chunk_count 表示语义向量缺失或不完整。
    chunk_count: int = 0
    embedded_count: int = 0
    # 用户已看过 OCR 估算并同意花这份额度
    ocr_approved: bool = False


@dataclass(frozen=True)
class Job:
    id: str
    type: str
    material_id: str
    course_id: str
    status: str
    stage: str
    progress: int
    error_message: str | None
    retrieval_backend: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Chunk:
    id: str
    material_id: str
    course_id: str
    ordinal: int
    page: int | None
    content: str
