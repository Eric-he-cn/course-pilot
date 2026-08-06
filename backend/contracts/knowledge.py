from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol
@dataclass(frozen=True)
class Citation:
    """一条可点开的来源。kind 分教材原文与知识页转述：后者没有页码，
    界面必须把两者标得不一样，用户才知道自己点开的是原文还是二手整理。"""
    material_id: str; document: str; page: int | None; chunk_id: str; snippet: str; score: float
    kind: str = "material"; concept_id: str = ""; concept_name: str = ""
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
# 手写区的身份标注。写检索行的一端与渲染给模型的一端都用它：知识页要按它拆出手写区
# 单独留位，两边各写一份措辞就会在某一天错开，拆分点随之失效。
HANDWRITTEN_LABEL = "【以下是用户自己写的补充，不是教材内容】"
@dataclass(frozen=True)
class WikiSource:
    """知识页转述时依据的一个教材位置。给的是整页的来源，不是「这句话在第几页」。
    material_id 是归属教材：文件名可以重名，分组与匹配要按它，不然两本同名书会并成一条。"""
    document: str; page: int | None; chunk_id: str; snippet: str; material_id: str = ""
@dataclass(frozen=True)
class WikiSources:
    """一页知识页的教材出处：anchors 是可点开的那几页，pages 是去重后的总页数。
    总览页依据的页可能成百上千，anchors 会截断，pages 让界面说得出截了多少。"""
    anchors: tuple[WikiSource, ...]; pages: int
class KnowledgeSearchPort(Protocol):
    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]: ...
    def search_wiki(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 2) -> list[KnowledgeHit]: ...
    def material_names(self, *, scope: ResolvedKnowledgeScope) -> list[str]: ...
    def concepts(self, *, scope: ResolvedKnowledgeScope, limit: int = 60) -> list[ConceptRef]: ...
    def wiki_enabled(self, *, scope: ResolvedKnowledgeScope) -> bool: ...
    def wiki_index(self, *, scope: ResolvedKnowledgeScope) -> list[WikiEntry]: ...
    def wiki_read(self, *, scope: ResolvedKnowledgeScope, concept_id: str) -> WikiDocument: ...
    def wiki_sources(self, *, scope: ResolvedKnowledgeScope, concept_id: str) -> WikiSources: ...
