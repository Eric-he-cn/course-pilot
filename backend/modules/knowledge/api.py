from __future__ import annotations

from typing import Protocol

from contracts.knowledge import KnowledgeHit

from .models import Job, Material


class CourseKnowledgeSettingsPort(Protocol):
    """Public dependency on courses; knowledge never reads course tables itself."""

    def wiki_is_enabled(self, *, course_id: str) -> bool: ...


class KnowledgeUseCases(Protocol):
    def upload_material(self, *, course_id: str, filename: str, mime_type: str, content: bytes) -> Material: ...
    def list_materials(self, *, course_id: str) -> list[Material]: ...
    def start_index(self, *, material_id: str) -> Job: ...
    def get_job(self, *, job_id: str) -> Job | None: ...
    def start_wiki_build(self, *, material_id: str) -> Job: ...
    def search_course(self, *, course_id: str, query: str, limit: int = 6) -> list[KnowledgeHit]: ...
    def health(self) -> dict[str, object]: ...


class KnowledgeFeatureDisabledError(ValueError):
    """Returned when a Wiki build is requested before its course flag is enabled."""


class MaterialNotIndexedError(ValueError):
    """A Wiki build requires already searchable material; it must not imply indexing."""
