from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from contracts.knowledge import KnowledgeHit, KnowledgeSearchPort, ResolvedKnowledgeScope
from contracts.llm import ToolSpec
from modules.learning.api import ArchiveReaderPort, EvidenceWriterPort
from modules.planning.api import PlanReaderPort
from modules.memory.store import MemoryStore
from modules.sessions.artifacts import ArtifactStore

from .skills import SkillRegistry

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
        description="读取当前课程学习档案：最近的证据事件、各概念掌握度与弱项，用于回答学习进度类问题。",
        parameters={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="concept_search",
        description=(
            "列出当前课程概念目录里的概念及其 id。归因证据前必须先调用它——"
            "concept_id 只能取自这里，目录外的概念一律用 topic_hint。"
        ),
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string", "description": "可选，按名称过滤"}},
        },
    ),
    ToolSpec(
        name="emit_evidence",
        description=(
            "把一次可判定的作答结果写成证据事件。掌握度数值由服务端的确定性算法更新，"
            "不要在参数里给分数或掌握程度。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["attempt_correct", "attempt_incorrect", "follow_up", "user_override"]},
                "concept_id": {"type": "string", "description": "必须来自 concept_search；拿不准就留空并给 topic_hint"},
                "topic_hint": {"type": "string", "description": "无法归因时用自然语言写考点，进人工补录队列"},
                "with_hint": {"type": "boolean", "description": "用户是在提示或重试后才答对"},
            },
            "required": ["kind"],
        },
    ),
    ToolSpec(
        name="artifact_read",
        description="读取本会话最近的跨轮产物（如练习题目与私有答案要点）。",
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "可选，按 kind 过滤，例如 practice"},
                "limit": {"type": "integer", "description": "默认 5，最多 20"},
            },
        },
    ),
    ToolSpec(
        name="artifact_append",
        description=(
            "写入一条跨轮产物。visibility=model_private 的内容不会展示给用户，"
            "适合存标准答案与评分要点；payload 结构由本 skill 自行约定。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "visibility": {"type": "string", "enum": ["user_visible", "model_private"]},
                "payload": {"type": "object", "description": "任意 JSON 对象"},
            },
            "required": ["kind", "visibility", "payload"],
        },
    ),
    ToolSpec(
        name="memory_patch",
        description=(
            "更新长期记忆的一个受管区块：user 记跨课程的学习偏好与目标，course 记这门课学到哪、"
            "遗留问题和与用户的约定。只写叙述性内容——掌握度数值、错题记录与复习排期不写这里。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["user", "course"]},
                "section": {"type": "string", "description": "区块名，小写字母数字下划线，如 preferences / progress"},
                "content": {"type": "string", "description": "该区块的完整新内容，会整块替换"},
            },
            "required": ["scope", "section", "content"],
        },
    ),
    ToolSpec(
        name="use_skill",
        description="加载一个专项能力的操作规程。需要组织练习（出题/评分/讲评/变式题）时调用 practice。",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "skill 名称"}},
            "required": ["name"],
        },
    ),
)

# 工具 profile（架构 §9.2）：skill 激活后切换到它的完整集合，而不是在主集合上做并集。
# search_materials / get_plan / get_archive 是 rag_search / plan_read / archive_query 的历史名。
MAIN_PROFILE = ("search_materials", "list_materials", "get_plan", "get_archive", "concept_search", "emit_evidence", "memory_patch", "use_skill")
_SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


def specs_for(allowed: tuple[str, ...]) -> tuple[ToolSpec, ...]:
    return tuple(_SPECS_BY_NAME[name] for name in allowed if name in _SPECS_BY_NAME)


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
                "number": number,
                "material_id": hit.citation.material_id,
                "document": hit.citation.document,
                "page": hit.citation.page,
                "chunk_id": hit.citation.chunk_id,
                "snippet": hit.citation.snippet,
                "score": hit.citation.score,
            }
        )
        return number, True


_CITATION_MARK = re.compile(r"\[(\d+)\]")


def cited_only(answer: str, citations: list[dict]) -> list[dict]:
    """引用列表只保留回答里真正标注过的编号：检索到但没用上的片段不算依据。"""
    used = {int(number) for number in _CITATION_MARK.findall(answer)}
    return [citation for citation in citations if citation["number"] in used]


@dataclass(frozen=True)
class ToolOutcome:
    text: str  # 回填给模型的 tool 消息正文
    ok: bool
    summary: str  # 面向用户的一句话结果，用于 SSE tool_result
    new_citations: list[dict] = field(default_factory=list)
    activated_skill: str | None = None  # use_skill 成功时带回，用于当轮切换工具 profile


class ToolExecutor:
    """工具只吃服务端解析出的课程 scope；模型无法指定 course_id。"""

    def __init__(
        self, *, knowledge: KnowledgeSearchPort, plans: PlanReaderPort, archive: ArchiveReaderPort,
        evidence: EvidenceWriterPort, artifacts: ArtifactStore, skills: SkillRegistry, memory: MemoryStore,
    ) -> None:
        self._knowledge = knowledge
        self._plans = plans
        self._archive = archive
        self._evidence = evidence
        self._artifacts = artifacts
        self._skills = skills
        self._memory = memory

    def execute(self, *, scope: ResolvedKnowledgeScope, session_id: str, name: str, arguments: str, registry: CitationRegistry, allowed: tuple[str, ...]) -> ToolOutcome:
        if name not in allowed:
            # 当轮最小权限：skill 激活期间看不到的工具，即使模型硬调也要拒绝。
            return ToolOutcome(text=f"当前不可使用工具 {name}。可用：" + "、".join(allowed), ok=False, summary="工具不可用")
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
            if name == "concept_search":
                return self._concepts(scope, parsed)
            if name == "emit_evidence":
                return self._emit_evidence(scope, parsed)
            if name == "artifact_read":
                return self._artifact_read(session_id, parsed)
            if name == "artifact_append":
                return self._artifact_append(scope, session_id, parsed)
            if name == "memory_patch":
                return self._memory_patch(scope, parsed)
            if name == "use_skill":
                return self._use_skill(parsed)
        except ValueError as error:
            return ToolOutcome(text=f"参数无效：{error}", ok=False, summary="参数无效")
        except Exception:
            return ToolOutcome(text=f"工具 {name} 执行失败，请基于已有信息回答。", ok=False, summary="执行失败")
        return ToolOutcome(text=f"没有名为 {name} 的工具。可用工具：" + "、".join(allowed), ok=False, summary="未知工具")

    def _concepts(self, scope: ResolvedKnowledgeScope, parsed: dict) -> ToolOutcome:
        keyword = str(parsed.get("keyword") or "").strip().casefold()
        concepts = self._knowledge.concepts(scope=scope)
        if keyword:
            concepts = [item for item in concepts if keyword in item.name.casefold()]
        if not concepts:
            return ToolOutcome(text="当前课程还没有概念目录（教材索引后自动生成）。", ok=True, summary="无概念")
        lines = [f"- {item.id} | {item.name}" + (f"（第 {item.page} 页）" if item.page else "") for item in concepts[:40]]
        return ToolOutcome(text="概念目录（归因只能用这些 id）：\n" + "\n".join(lines), ok=True, summary=f"{len(concepts)} 个概念")

    def _emit_evidence(self, scope: ResolvedKnowledgeScope, parsed: dict) -> ToolOutcome:
        kind = str(parsed.get("kind") or "").strip()
        concept_id = str(parsed.get("concept_id") or "").strip() or None
        topic_hint = str(parsed.get("topic_hint") or "").strip() or None
        payload = {"with_hint": bool(parsed.get("with_hint"))}
        event = self._evidence.record_evidence(
            course_id=scope.course_id, kind=kind, concept_id=concept_id, topic_hint=topic_hint, payload=payload,
        )
        if event.attribution_status == "attributed":
            return ToolOutcome(text=f"已记录证据事件 {event.kind}（概念 {event.concept_id}），掌握度已更新。", ok=True, summary=f"证据 {kind}")
        return ToolOutcome(
            text=f"已记录为未归因证据（topic_hint={event.topic_hint}）：概念不在目录里，不更新掌握度。",
            ok=True, summary=f"证据 {kind}（未归因）",
        )

    def _artifact_read(self, session_id: str, parsed: dict) -> ToolOutcome:
        kind = str(parsed.get("kind") or "").strip() or None
        limit = int(parsed.get("limit") or 5)
        items = self._artifacts.recent(session_id=session_id, kind=kind, limit=limit)
        if not items:
            return ToolOutcome(text="本会话还没有相关产物。", ok=True, summary="无产物")
        blocks = [
            f"[{item.created_at}] id={item.id} kind={item.kind} visibility={item.visibility}\n{json.dumps(item.payload, ensure_ascii=False)}"
            for item in items
        ]
        return ToolOutcome(text="\n\n".join(blocks), ok=True, summary=f"读到 {len(items)} 条产物")

    def _artifact_append(self, scope: ResolvedKnowledgeScope, session_id: str, parsed: dict) -> ToolOutcome:
        payload = parsed.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是 JSON 对象")
        item = self._artifacts.append(
            course_id=scope.course_id, session_id=session_id, kind=str(parsed.get("kind") or ""),
            visibility=str(parsed.get("visibility") or ""), payload=payload,
        )
        return ToolOutcome(text=f"已保存产物 {item.id}（kind={item.kind}, visibility={item.visibility}）。", ok=True, summary=f"存 {item.kind}")

    def _memory_patch(self, scope: ResolvedKnowledgeScope, parsed: dict) -> ToolOutcome:
        message = self._memory.patch(
            scope=str(parsed.get("scope") or ""), section=str(parsed.get("section") or ""),
            content=str(parsed.get("content") or ""), course_id=scope.course_id,
        )
        return ToolOutcome(text=message, ok=True, summary=message)

    def _use_skill(self, parsed: dict) -> ToolOutcome:
        name = str(parsed.get("name") or "").strip()
        skill = self._skills.get(name)
        if skill is None:
            available = "、".join(self._skills.names()) or "（无）"
            return ToolOutcome(text=f"没有名为 {name} 的 skill。可用：{available}", ok=False, summary="未知 skill")
        return ToolOutcome(
            text=f"# Skill: {skill.name}\n\n{skill.body}", ok=True, summary=f"加载 {skill.name}",
            activated_skill=skill.name,
        )

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
