from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
@dataclass(frozen=True)
class Citation:
    material_id: str; document: str; page: int | None; chunk_id: str; snippet: str; score: float
@dataclass(frozen=True)
class KnowledgeHit:
    citation: Citation; content: str


@dataclass(frozen=True)
class ResolvedKnowledgeScope:
    """Server-issued scope.  Models and HTTP payloads cannot supply this value."""
    turn_id: str
    course_id: str
    resolver_version: str
@dataclass(frozen=True)
class ConceptRef:
    """概念目录条目：归因时模型只能从这里选，列表外的概念一律 unattributed。"""
    id: str; name: str; page: int | None
class KnowledgeSearchPort(Protocol):
    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]: ...
    def material_names(self, *, scope: ResolvedKnowledgeScope) -> list[str]: ...
    def concepts(self, *, scope: ResolvedKnowledgeScope, limit: int = 60) -> list[ConceptRef]: ...
