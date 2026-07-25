from __future__ import annotations

import json
from dataclasses import dataclass, field

from contracts.knowledge import KnowledgeHit, KnowledgeSearchPort, ResolvedKnowledgeScope
from contracts.llm import ToolSpec
from modules.learning.api import ArchiveReaderPort
from modules.planning.api import PlanReaderPort

SEARCH_LIMIT = 6

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_materials",
        description=(
            "在当前课程的教材资料库中检索相关内容。系统已用用户原话检索过一次；"
            "当已有证据不足、用户追问细节、或需要换关键词（含中英互译）时再调用。"
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索查询词，可以与用户原话不同"}},
            "required": ["query"],
        },
    ),
    ToolSpec(
        name="list_materials",
        description="列出当前课程资料库中的教材文件名。",
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="get_plan",
        description="读取当前课程的学习计划（由服务端持久化，可能不存在）。",
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="get_archive",
        description="读取当前课程学习档案中最近的证据事件，用于回答学习进度类问题。",
        parameters={"type": "object", "properties": {}},
    ),
)


class CitationRegistry:
    """整轮统一的引用编号：同一 chunk 在多次检索中命中共用一个编号。"""

    def __init__(self) -> None:
        self._by_chunk: dict[str, int] = {}
        self.citations: list[dict] = []

    def register(self, hit: KnowledgeHit) -> tuple[int, bool]:
        existing = self._by_chunk.get(hit.citation.chunk_id)
        if existing is not None:
            return existing, False
        number = len(self.citations) + 1
        self._by_chunk[hit.citation.chunk_id] = number
        self.citations.append(
            {
                "citation_id": f"citation_{number}",
                "material_id": hit.citation.material_id,
                "document": hit.citation.document,
                "page": hit.citation.page,
                "chunk_id": hit.citation.chunk_id,
                "snippet": hit.citation.snippet,
                "score": hit.citation.score,
            }
        )
        return number, True


@dataclass(frozen=True)
class ToolOutcome:
    text: str  # 回填给模型的 tool 消息正文
    ok: bool
    summary: str  # 面向用户的一句话结果，用于 SSE tool_result
    new_citations: list[dict] = field(default_factory=list)


class ToolExecutor:
    """工具只吃服务端解析出的课程 scope；模型无法指定 course_id。"""

    def __init__(self, *, knowledge: KnowledgeSearchPort, plans: PlanReaderPort, archive: ArchiveReaderPort) -> None:
        self._knowledge = knowledge
        self._plans = plans
        self._archive = archive

    def execute(self, *, scope: ResolvedKnowledgeScope, name: str, arguments: str, registry: CitationRegistry) -> ToolOutcome:
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
            if not isinstance(parsed, dict):
                raise ValueError("arguments 必须是 JSON 对象")
        except (json.JSONDecodeError, ValueError):
            return ToolOutcome(text="工具参数不是合法的 JSON 对象，请修正后重试。", ok=False, summary="参数无效")
        try:
            if name == "search_materials":
                return self._search(scope, parsed, registry)
            if name == "list_materials":
                names = self._knowledge.material_names(scope=scope)
                return ToolOutcome(text="课程资料库文件：" + ("、".join(names) if names else "（尚未上传教材）"), ok=True, summary=f"{len(names)} 份资料")
            if name == "get_plan":
                return self._plan(scope)
            if name == "get_archive":
                return self._archive_events(scope)
        except Exception:
            return ToolOutcome(text=f"工具 {name} 执行失败，请基于已有信息回答。", ok=False, summary="执行失败")
        return ToolOutcome(text=f"没有名为 {name} 的工具。可用工具：" + "、".join(spec.name for spec in TOOL_SPECS), ok=False, summary="未知工具")

    def _search(self, scope: ResolvedKnowledgeScope, parsed: dict, registry: CitationRegistry) -> ToolOutcome:
        query = str(parsed.get("query") or "").strip()
        if not query:
            return ToolOutcome(text="search_materials 需要非空的 query 参数。", ok=False, summary="缺少查询词")
        hits = self._knowledge.search(scope=scope, query=query, limit=SEARCH_LIMIT)
        if not hits:
            return ToolOutcome(text="（未检索到相关教材内容；可换关键词再查一次）", ok=True, summary=f"检索「{_clip(query, 24)}」未命中")
        blocks, new_citations = [], []
        for hit in hits:
            number, is_new = registry.register(hit)
            if is_new:
                new_citations.append(registry.citations[number - 1])
            page = f"，第 {hit.citation.page} 页" if hit.citation.page is not None else ""
            blocks.append(f"[{number}] 文档：{hit.citation.document}{page}；片段：{hit.citation.chunk_id}\n{hit.content}")
        return ToolOutcome(text="\n\n".join(blocks), ok=True, summary=f"检索「{_clip(query, 24)}」命中 {len(hits)} 段", new_citations=new_citations)

    def _plan(self, scope: ResolvedKnowledgeScope) -> ToolOutcome:
        plan = self._plans.get_plan(course_id=scope.course_id)
        if plan is None:
            return ToolOutcome(text="该课程还没有学习计划。", ok=True, summary="暂无计划")
        lines = [f"- {item.due_date} {item.title}（{item.status}）" for item in plan.items[:20]]
        return ToolOutcome(text=f"当前计划（版本 v{plan.version}，共 {len(plan.items)} 项）：\n" + "\n".join(lines), ok=True, summary=f"计划 {len(plan.items)} 项")

    def _archive_events(self, scope: ResolvedKnowledgeScope) -> ToolOutcome:
        archive = self._archive.get_archive(course_id=scope.course_id, limit=20)
        if not archive.events:
            return ToolOutcome(text="学习档案还没有证据事件。", ok=True, summary="档案为空")
        lines = [f"- {event.created_at} {event.kind}：{event.concept_id or event.topic_hint or '未归因'}（{event.attribution_status}）" for event in archive.events]
        return ToolOutcome(text=f"最近证据事件（共 {archive.evidence_count} 条）：\n" + "\n".join(lines), ok=True, summary=f"档案 {archive.evidence_count} 条事件")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
