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
@dataclass(frozen=True)
class WikiEntry:
    """知识页索引的一条。当前页面是平的；有了层级之后在这里加父子字段。"""
    concept_id: str; concept_name: str; chars: int
@dataclass(frozen=True)
class WikiDocument:
    """一页知识页，按落盘格式拆好：body 是系统按教材生成的，handwritten 是用户自己写的。
    拆分放在 knowledge 模块，页面格式（frontmatter、分隔标记）不外泄给调用方。"""
    concept_id: str; concept_name: str; body: str; handwritten: str
class KnowledgeSearchPort(Protocol):
    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]: ...
    def material_names(self, *, scope: ResolvedKnowledgeScope) -> list[str]: ...
    def concepts(self, *, scope: ResolvedKnowledgeScope, limit: int = 60) -> list[ConceptRef]: ...
    def wiki_enabled(self, *, scope: ResolvedKnowledgeScope) -> bool: ...
    def wiki_index(self, *, scope: ResolvedKnowledgeScope) -> list[WikiEntry]: ...
    def wiki_read(self, *, scope: ResolvedKnowledgeScope, concept_id: str) -> WikiDocument: ...
