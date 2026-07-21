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
class KnowledgeSearchPort(Protocol):
    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]: ...
