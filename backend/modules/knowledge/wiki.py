"""Course Wiki：按教材目录自底向上把一整本书写成一棵知识页。

这条路径上没有检索。叶子页读它那一节页码范围内的全部原文，中间页读它子页的正文，
根页 index.md 读全部顶层页。所以「一个概念在十处讲了、只召回得到六条」这件事不会发生；
真正的限制只剩节点上限，那个必须在提示里说出来。

三条硬约束不变：

1. **只用教材原文**。写不出来就少写一条，不让模型拿通用知识补。
2. **增量刷新**。证据没变就不重写，省 token 也省得每次生成一个不一样的版本。
3. **手写区不动**。分隔线以下是用户自己写的，重新生成只换上半部分。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from contracts.knowledge import WikiDocument
from contracts.llm import ChatFinal, ChatMessage

PROMPT_VERSION = "wiki-v2"
# 分隔线以下归用户。重新生成只替换上半部分，手写内容不会被冲掉。
HANDWRITTEN_MARKER = "<!-- 以下是手写区，重新生成不会覆盖 -->"
# 课程首页的固定 id。它是课程级的，不对应任何概念，清理孤儿页时要放过它。
INDEX_ID = "index"
# 树的最大深度，index.md 算第一层，所以概念最多用到三层。
WIKI_MAX_DEPTH = 4
# 单次构建最多写多少页。先小规模试跑，确认层级和页面质量之后再往上调。
WIKI_MAX_NODES = 50
# 一页最多读多少字原文。超过就按分片顺序再切一层，不截断——截断就是漏。
MAX_EVIDENCE_CHARS = 6000
MAX_PAGE_BYTES = 128 * 1024
_ALLOWED = re.compile(r"[^\w一-鿿\-_.]", re.UNICODE)
_TITLE_LINE = re.compile(r"^\s*(?:#+\s*)?标题[：:]\s*(.+)$")

_LEAF_SYSTEM = """你在为一门课的知识库写一页小节说明，读者是正在学这门课的学生。

下面给的是这一节在教材里的**全部原文**，按页码顺序排好。只依据它来写。规则：
- 每个结论后面标出出处。每段原文的开头都给了它的标签（形如【p.12】或【笔记.docx】），
  照抄那个标签放进方括号里，例如 [p.12]、[笔记.docx]。不要自己编 [p.未标页] 这类写法。
- 原文没有覆盖到的内容，**整条不要写**。宁可少写一条，也不要写「教材未覆盖」之后再
  用你自己的知识补一句——那样读者分不清哪句有出处。
- 不要复述整段原文，用自己的话把这一节讲清楚。
- 用 markdown。结构：一句话概括 → 关键点（3-6 条）→ 常见误解或易错点（有就写，没有就省略）。
- 不要写标题行（# 小节名），调用方会加。
- 全文控制在 600 字以内。"""

_LEAF_UNNAMED = """
这一节没有现成的名字。正文之前先单起一行写「标题：」加上你读完这段之后给它起的名字，
名字用 4-15 个字概括这段讲什么，不要用「第一段」这类没有信息的名字。"""

_BRANCH_SYSTEM = """你在为一门课的知识库写一页章节总览，读者是正在学这门课的学生。

下面给的是这一章各个子小节**已经写好的知识页正文**。只依据它们来写。规则：
- 说清这一章整体在讲什么，以及子小节之间是什么关系（谁是谁的前提、谁是谁的特例、
  谁解决了谁留下的问题）。这是总览页存在的意义，逐条复述子页没有价值。
- **不要标教材页码引用**。你读的是子页不是原文，页码你无从核对。要指路就写子页的名字。
- 用 markdown。不要写标题行，调用方会加。
- 全文控制在 500 字以内。"""

_INDEX_SYSTEM = """你在为一门课的知识库写首页，读者是刚打开这门课的学生。

下面给的是这门课各个顶层页面**已经写好的正文**。只依据它们来写。规则：
- 说清这门课整体讲什么、分成哪几大块、几大块之间怎么衔接，给出一条建议的阅读顺序。
- **不要标教材页码引用**，也不要自己列页面目录——目录由调用方附在后面。
- 用 markdown。不要写标题行。全文控制在 400 字以内。"""

# 首页末尾附的页面目录。由落盘的页面清单生成，不经过模型，列不出不存在的页。
_INDEX_DIRECTORY_HEADING = "## 全部页面"


@dataclass(frozen=True)
class WikiPage:
    concept_id: str
    concept_name: str
    source_hash: str
    updated_at: str
    chars: int
    material_id: str = ""
    parent_id: str = ""
    level: int = 0
    order: int = 0


@dataclass
class Section:
    """一页知识页对应的教材片段。有子页的是中间页，它不读原文。"""
    id: str
    name: str  # 空串表示让模型读完这段自己起名
    level: int
    parent_id: str | None
    first_page: int | None
    last_page: int | None
    chunks: list[dict] = field(default_factory=list)
    children: list[str] = field(default_factory=list)


def _safe(component: str) -> str:
    return _ALLOWED.sub("", component).strip()[:120]


class WikiStore:
    """按课程隔离的 markdown 落盘，落点校验照 NoteStore 的口径。"""

    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir / "wiki"

    def _course_dir(self, course_id: str) -> Path:
        return self._root / _safe(course_id)

    def _path(self, *, course_id: str, concept_id: str) -> Path:
        directory = self._course_dir(course_id)
        name = _safe(concept_id)
        if not name:
            raise ValueError("概念 id 不合法")
        path = (directory / f"{name}.md").resolve()
        base = directory.resolve()
        if os.path.commonpath([str(base), str(path)]) != str(base):
            raise ValueError("Wiki 页只能写在本课程目录内")
        if path.is_symlink():
            raise ValueError("Wiki 页是符号链接，已拒绝写入")
        return path

    def read(self, *, course_id: str, concept_id: str) -> str:
        path = self._path(course_id=course_id, concept_id=concept_id)
        if not path.is_file():
            raise LookupError(f"没有 {concept_id} 的 Wiki 页")
        return path.read_text(encoding="utf-8")

    def source_hash(self, *, course_id: str, concept_id: str) -> str:
        """读已有页的证据指纹，用来判断要不要重写。读不到就返回空串。"""
        try:
            head = self.read(course_id=course_id, concept_id=concept_id)[:800]
        except (LookupError, ValueError):
            return ""
        match = re.search(r"^source_hash:[ \t]*(\S+)$", head, re.MULTILINE)
        return match.group(1) if match else ""

    def write(self, *, course_id: str, concept_id: str, concept_name: str, body: str,
              source_hash: str, source_refs: list[str], updated_at: str,
              material_id: str = "", parent_id: str | None = None, level: int = 0,
              order: int = 0) -> WikiPage:
        path = self._path(course_id=course_id, concept_id=concept_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        handwritten = ""
        if path.is_file():
            existing = path.read_text(encoding="utf-8")
            if HANDWRITTEN_MARKER in existing:
                handwritten = HANDWRITTEN_MARKER + existing.split(HANDWRITTEN_MARKER, 1)[1]
        refs = "\n".join(f"  - {ref}" for ref in source_refs) or "  []"
        # 掌握度不写进文件：它随答题变化，读的时候现算才不会过期（架构 §8.2）
        document = (
            f"---\nconcept_id: {concept_id}\nconcept_name: {concept_name}\n"
            f"material_id: {material_id}\nparent_id: {parent_id or ''}\n"
            f"level: {level}\norder: {order}\n"
            f"source_hash: {source_hash}\nprompt_version: {PROMPT_VERSION}\n"
            f"updated_at: {updated_at}\nsource_refs:\n{refs}\n---\n\n"
            f"# {concept_name}\n\n{body.strip()}\n\n{handwritten or HANDWRITTEN_MARKER + chr(10)}"
        )
        if len(document.encode("utf-8")) > MAX_PAGE_BYTES:
            raise ValueError("Wiki 页超过大小上限")
        path.write_text(document, encoding="utf-8")
        return WikiPage(concept_id, concept_name, source_hash, updated_at, len(document),
                        material_id, parent_id or "", level, order)

    def list_pages(self, *, course_id: str) -> list[WikiPage]:
        """按构建时记下的 order 返回，首页排在最前。文件名是 id，照文件名排等于随机排。"""
        directory = self._course_dir(course_id)
        pages: list[WikiPage] = []
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            head = path.read_text(encoding="utf-8")[:800]

            def field_of(key: str, text: str = head) -> str:
                # 空值那几行不能用 \s*：它会吃掉换行，把下一行整行当成本行的值。
                match = re.search(rf"^{key}:[ \t]*(.*)$", text, re.MULTILINE)
                return match.group(1).strip() if match else ""

            pages.append(WikiPage(
                concept_id=field_of("concept_id") or path.stem, concept_name=field_of("concept_name") or path.stem,
                source_hash=field_of("source_hash"), updated_at=field_of("updated_at"), chars=path.stat().st_size,
                material_id=field_of("material_id"), parent_id=field_of("parent_id"),
                level=int(field_of("level") or 0), order=int(field_of("order") or 0),
            ))
        return sorted(pages, key=lambda page: (page.concept_id != INDEX_ID, page.order, page.concept_id))

    def prune(self, *, course_id: str, valid_concept_ids: set[str], material_id: str = "",
              planned_ids: set[str] | None = None, known_material_ids: set[str] | None = None) -> list[str]:
        """删掉不再属于这门课的页。概念表与这次的构建计划是真源。

        本次重建的那份教材按计划对账，别的教材只要还在就保留它的页；剩下的（老版本写的、
        没记教材归属的）照概念表判断。首页是课程级的，任何时候都不删。
        """
        planned = planned_ids or set()
        alive = known_material_ids or set()
        directory = self._course_dir(course_id)
        removed = []
        for page in self.list_pages(course_id=course_id):
            if page.concept_id == INDEX_ID:
                continue
            if page.material_id and page.material_id == material_id:
                keep = page.concept_id in planned
            elif page.material_id:
                keep = page.material_id in alive
            else:
                keep = page.concept_id in valid_concept_ids
            if not keep:
                (directory / f"{_safe(page.concept_id)}.md").unlink(missing_ok=True)
                removed.append(page.concept_id)
        return removed

    def delete_course(self, *, course_id: str) -> None:
        """删课程时由组装根调用。目录布局是本模块自己的事，别处不该知道。"""
        shutil.rmtree(self._course_dir(course_id), ignore_errors=True)


def split_page(*, concept_id: str, document: str) -> WikiDocument:
    """把落盘的一页拆成正文与手写区，丢掉 frontmatter。

    frontmatter 记的是证据指纹、提示词版本这类内部账，读页的人不需要；标题行也去掉，
    概念名单独给出。手写区要留着——那是用户自己整理的内容，但归属得说清楚。
    """
    text = re.sub(r"\A---\n.*?\n---\n", "", document, count=1, flags=re.S).strip()
    body, _, handwritten = text.partition(HANDWRITTEN_MARKER)
    concept_name = ""
    if match := re.match(r"#\s+(.+)", body.strip()):
        concept_name = match.group(1).strip()
        body = body.strip()[match.end():]
    return WikiDocument(concept_id, concept_name or concept_id, body.strip(), handwritten.strip())


# ---- 切段：把一份教材切成一棵 Section 树 ----

def _section_id(material_id: str, ordinal: int) -> str:
    """按教材加分片序号派生，重建索引后同一段仍是同一个 id，增量刷新才认得出来。"""
    return "section_" + hashlib.sha1(f"{material_id}\n{ordinal}".encode()).hexdigest()[:16]


def _groups_by_size(chunks: list[dict], *, target: int) -> list[list[dict]]:
    """按分片顺序攒段，攒到 target 为止。单个超长分片自己成一段，绝不丢。"""
    groups: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for chunk in chunks:
        length = len(chunk["content"])
        if current and size + length > target:
            groups.append(current)
            current, size = [], 0
        current.append(chunk)
        size += length
    if current:
        groups.append(current)
    return groups


def _merge_to_fit(groups: list[list[dict]], max_nodes: int) -> list[list[dict]]:
    """段数还是超上限时并掉多出来的那几段，每次挑最短的一对相邻段。

    只并到刚好装下，不是两两减半——减半会把段撑到远超必要的长度。
    合并只让段变长，不会漏掉分片。
    """
    def size(group: list[dict]) -> int:
        return sum(len(chunk["content"]) for chunk in group)

    while len(groups) > max_nodes >= 1:
        pick = min(range(len(groups) - 1), key=lambda index: size(groups[index]) + size(groups[index + 1]))
        groups[pick : pick + 2] = [groups[pick] + groups[pick + 1]]
    return groups


def _group_section(material_id: str, group: list[dict], *, level: int, parent_id: str | None) -> Section:
    pages = [chunk["page"] for chunk in group if chunk["page"]]
    return Section(id=_section_id(material_id, group[0]["ordinal"]), name="", level=level,
                   parent_id=parent_id, first_page=min(pages, default=None),
                   last_page=max(pages, default=None), chunks=list(group))


def _by_chunk_order(*, material_id: str, chunks: list[dict], max_nodes: int,
                    ) -> tuple[list[Section], dict[str, int]]:
    """没有可用目录时的切法：按分片顺序切成等大的段，段名让模型读完自己起。

    讲义、扫描件、没做书签的 PDF 都走这条路，它不是兜底——多数真实教材没有书签。
    这条路没有上级页接住被砍掉的段，所以段数超过节点上限时**把段放大到装得下**，
    而不是丢掉尾巴：原文一页都不能漏是这次改造的全部意义。代价是每页要读的原文变多，
    合并了多少如实报出来，让调用方知道该提高上限。
    """
    natural = _groups_by_size(chunks, target=MAX_EVIDENCE_CHARS)
    groups = natural
    if max_nodes >= 1 and len(natural) > max_nodes:
        total = sum(len(chunk["content"]) for chunk in chunks)
        groups = _merge_to_fit(_groups_by_size(chunks, target=max(1, -(-total // max_nodes))), max_nodes)
    sections = [_group_section(material_id, group, level=0, parent_id=None) for group in groups]
    return sections, {"candidates": len(natural), "capped": len(natural) - len(groups), "dropped": 0}


def plan_sections(
    *, material_id: str, concepts: list[dict], chunks: list[dict],
    max_nodes: int = WIKI_MAX_NODES, max_depth: int = WIKI_MAX_DEPTH,
) -> tuple[list[Section], dict[str, int]]:
    """把一份教材切成一棵 Section 树，父节点排在子节点前面。

    `concepts` 是这份教材的概念目录，按教材里的先后排好；带 level 的来自 PDF 书签。
    返回值第二项报候选节点数与被上限砍掉的数量，调用方要把它说给用户听。
    """
    chunks = sorted(chunks, key=lambda chunk: chunk["ordinal"])
    outline = [row for row in concepts if row.get("level") is not None]
    # 一页页码都没有就切不出区间（无文字层的 PDF 兜底提取就是这样），退回按分片切段。
    if not outline or not any(chunk["page"] for chunk in chunks):
        return _by_chunk_order(material_id=material_id, chunks=chunks, max_nodes=max_nodes)

    total_pages = max((chunk["page"] for chunk in chunks if chunk["page"]), default=0)
    known = {row["id"] for row in outline}
    children: dict[str | None, list[str]] = {}
    for row in outline:
        parent = row.get("parent_id") if row.get("parent_id") in known else None
        children.setdefault(parent, []).append(row["id"])

    # 广度优先定去留：上限砍掉的是最深的那批，章节级的页不会整段消失，
    # 而它们的页码区间会被上级页接过去，覆盖不因为上限出现空洞。
    depth: dict[str, int] = {}
    order: list[str] = []
    queue = [(node_id, 0) for node_id in children.get(None, [])]
    while queue:
        node_id, level = queue.pop(0)
        if level > max_depth - 2:  # index.md 占掉一层
            continue
        depth[node_id] = level
        order.append(node_id)
        queue.extend((child_id, level + 1) for child_id in children.get(node_id, []))
    kept = set(order[:max_nodes])
    doc = [row for row in outline if row["id"] in kept]
    if not doc:
        return _by_chunk_order(material_id=material_id, chunks=chunks, max_nodes=max_nodes)

    first_child = {}
    for row in doc:
        parent = row.get("parent_id") if row.get("parent_id") in kept else None
        if parent is not None:
            first_child.setdefault(parent, row["id"])

    starts: dict[str, int] = {}
    previous = 1
    for position, row in enumerate(doc):
        page = row.get("page") or previous  # 书签指不到页时贴着上一节，不留缝
        parent = row.get("parent_id") if row.get("parent_id") in kept else None
        # 章节自己那几页导语在第一个子节点之前，让首个子节点从父节点起算才不会没人读。
        if parent is not None and first_child.get(parent) == row["id"]:
            page = min(page, starts.get(parent, page))
        starts[row["id"]] = 1 if position == 0 else page  # 第一个节点之前的封面页也归它
        previous = page

    ends: dict[str, int] = {}
    for position, row in enumerate(doc):
        following = doc[position + 1] if position + 1 < len(doc) else None
        # 书签页码指的是标题所在页，跨页标题会指到上一页。结尾多带一页，宁可相邻
        # 小节重叠，也不让边界那页的正文谁都没读到。
        end = (following.get("page") or starts[following["id"]]) if following else total_pages
        # 目录里页码倒退时（扫描件重排、附录插在中间）区间会起点大于终点，那一节
        # 一个分片都取不到。夹到起始页，它至少读得到自己那一页。
        ends[row["id"]] = max(end, starts[row["id"]])

    sections: list[Section] = []
    for row in doc:
        kids = [child_id for child_id in children.get(row["id"], []) if child_id in kept]
        first, last = starts[row["id"]], ends[row["id"]]
        sections.append(Section(
            id=row["id"], name=row["name"], level=depth[row["id"]],
            parent_id=row.get("parent_id") if row.get("parent_id") in kept else None,
            first_page=first, last_page=last,
            chunks=[] if kids else _in_pages(chunks, first, last), children=kids,
        ))

    _split_oversized(sections, material_id=material_id, max_nodes=max_nodes, max_depth=max_depth)
    # 上限砍掉的节点由上级页接过它的页码区间，所以只是「没单独成页」，不是没读到。
    return sections, {"candidates": len(outline), "capped": len(outline) - len(doc), "dropped": 0}


def _in_pages(chunks: list[dict], first: int, last: int) -> list[dict]:
    return [chunk for chunk in chunks if chunk["page"] and first <= chunk["page"] <= last]


def _split_oversized(sections: list[Section], *, material_id: str, max_nodes: int, max_depth: int) -> None:
    """一节的原文一次读不完就按分片顺序再切一层。切不动就整段照读，不截断。"""
    for section in list(sections):
        if section.children or section.level + 1 > max_depth - 2:
            continue
        if sum(len(chunk["content"]) for chunk in section.chunks) <= MAX_EVIDENCE_CHARS:
            continue
        groups = _groups_by_size(section.chunks, target=MAX_EVIDENCE_CHARS)
        if len(groups) < 2 or len(sections) + len(groups) > max_nodes:
            continue
        position = sections.index(section) + 1
        for offset, group in enumerate(groups):
            child = _group_section(material_id, group, level=section.level + 1, parent_id=section.id)
            section.children.append(child.id)
            sections.insert(position + offset, child)
        section.chunks = []


# ---- 生成：自底向上写页 ----

def _fingerprint(evidence: str) -> str:
    return hashlib.sha1(evidence.encode("utf-8")).hexdigest()[:16]


def _raw_evidence(section: Section, document: str) -> tuple[str, list[str]]:
    """叶子页的证据：这一节页码范围内的全部原文，按顺序拼起来。"""
    blocks, refs = [], []
    for chunk in section.chunks:
        where = document + (f" p.{chunk['page']}" if chunk["page"] else "")
        blocks.append(f"【{where}】\n{chunk['content'].strip()}")
        refs.append(f"{where} #{chunk['id']}")
    return "\n\n".join(blocks), refs


def _child_evidence(section: Section, bodies: dict[str, str], names: dict[str, str]) -> tuple[str, list[str]]:
    """中间页的证据：子页正文。这里不碰原文，页码引用也就无从谈起。"""
    blocks, refs = [], []
    for child_id in section.children:
        body = bodies.get(child_id)
        if not body:
            continue
        name = names.get(child_id, child_id)
        blocks.append(f"【子页：{name}】\n{body}")
        refs.append(f"子页 {name} <{child_id}>")
    return "\n\n".join(blocks), refs


def _titled(name: str, body: str) -> tuple[str, str]:
    """段名留给模型起的那种页，把它写的标题行摘出来，正文里不重复留一份。"""
    if name:
        return name, body
    lines = body.lstrip().splitlines()
    if lines and (match := _TITLE_LINE.match(lines[0])):
        return match.group(1).strip()[:60], "\n".join(lines[1:]).strip()
    return "未命名小节", body


def _directory(pages: list[WikiPage]) -> str:
    """首页末尾的页面目录。由落盘清单生成，列不出不存在的页。"""
    lines = [f"{'  ' * page.level}- {page.concept_name}" for page in pages if page.concept_id != INDEX_ID]
    return f"{_INDEX_DIRECTORY_HEADING}\n\n" + "\n".join(lines)


def build_pages(
    *, course_id: str, material_id: str, document: str, sections: list[Section],
    store: WikiStore, now: str, ask: Callable[[list[ChatMessage]], ChatFinal],
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """自底向上写页，最后写课程首页。返回「写入 / 跳过 / 无内容」三个计数。"""
    counts = {"written": 0, "skipped": 0, "ungrounded": 0}
    bodies: dict[str, str] = {}
    names: dict[str, str] = {}
    positions = {section.id: index for index, section in enumerate(sections)}
    total = len(sections) + 1
    done = 0

    # 深的先写：中间页要读子页的正文，子页得先在手上。
    for section in sorted(sections, key=lambda item: -item.level):
        done += 1
        if on_progress is not None:
            on_progress(done, total)
        if section.children:
            evidence, refs = _child_evidence(section, bodies, names)
            system = _BRANCH_SYSTEM
        else:
            evidence, refs = _raw_evidence(section, document)
            system = _LEAF_SYSTEM + ("" if section.name else _LEAF_UNNAMED)
        if not evidence.strip():
            counts["ungrounded"] += 1
            continue
        source_hash = _fingerprint(evidence)
        if store.source_hash(course_id=course_id, concept_id=section.id) == source_hash:
            counts["skipped"] += 1
            existing = split_page(concept_id=section.id,
                                  document=store.read(course_id=course_id, concept_id=section.id))
            bodies[section.id], names[section.id] = existing.body, existing.concept_name
            continue
        heading = section.name or "（这一段还没有名字）"
        final = ask([
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=f"小节：{heading}\n\n{evidence}"),
        ])
        body = (final.text or "").strip()
        if not body:
            counts["ungrounded"] += 1
            continue
        name, body = _titled(section.name, body)
        store.write(course_id=course_id, concept_id=section.id, concept_name=name, body=body,
                    source_hash=source_hash, source_refs=refs, updated_at=now,
                    material_id=material_id, parent_id=section.parent_id, level=section.level,
                    order=positions[section.id])
        bodies[section.id], names[section.id] = body, name
        counts["written"] += 1

    if on_progress is not None:
        on_progress(total, total)
    _write_index(course_id=course_id, store=store, now=now, ask=ask, counts=counts)
    return counts


def _write_index(*, course_id: str, store: WikiStore, now: str,
                 ask: Callable[[list[ChatMessage]], ChatFinal], counts: dict[str, int]) -> None:
    """课程首页读全部顶层页。按落盘清单来，同一门课的几份教材都会进目录。"""
    pages = [page for page in store.list_pages(course_id=course_id) if page.concept_id != INDEX_ID]
    if not pages:
        return
    blocks = []
    for page in pages:
        if page.level != 0:
            continue
        top = split_page(concept_id=page.concept_id,
                         document=store.read(course_id=course_id, concept_id=page.concept_id))
        blocks.append(f"【{top.concept_name}】\n{top.body}")
    directory = _directory(pages)
    evidence = "\n\n".join(blocks) or directory
    source_hash = _fingerprint(evidence + directory)
    if store.source_hash(course_id=course_id, concept_id=INDEX_ID) == source_hash:
        counts["skipped"] += 1
        return
    final = ask([
        ChatMessage(role="system", content=_INDEX_SYSTEM),
        ChatMessage(role="user", content=f"顶层页面：\n\n{evidence}"),
    ])
    body = (final.text or "").strip()
    if not body:
        counts["ungrounded"] += 1
        return
    store.write(course_id=course_id, concept_id=INDEX_ID, concept_name="课程总览",
                body=f"{body}\n\n{directory}", source_hash=source_hash,
                source_refs=[f"顶层页 {page.concept_name}" for page in pages if page.level == 0],
                updated_at=now, level=0, order=-1)
    counts["written"] += 1


def coverage_summary(counts: dict[str, int]) -> str:
    """构建结果的机器可读汇总，界面按字段渲染中英两版。

    覆盖率必须说出来：节点上限之下写出的仍然是这本书的一部分，静默截断读起来像写全了。
    """
    return ("wiki_coverage "
            f"concepts={counts.get('candidates', 0)} "
            f"pages={counts.get('written', 0) + counts.get('skipped', 0)} "
            f"written={counts.get('written', 0)} skipped={counts.get('skipped', 0)} "
            f"merged={counts.get('capped', 0)} dropped={counts.get('dropped', 0)} "
            f"empty={counts.get('ungrounded', 0)} pruned={counts.get('pruned', 0)}")
