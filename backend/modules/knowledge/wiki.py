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
import threading
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from statistics import median
from typing import Callable

from contracts.knowledge import HANDWRITTEN_LABEL, WikiDocument
from contracts.llm import ChatFinal, ChatMessage
from core.common import write_text_atomic

from .api import WikiPageTooLargeError

PROMPT_VERSION = "wiki-v2"
# 分隔线以下归用户。重新生成只替换上半部分，手写内容不会被冲掉。
HANDWRITTEN_MARKER = "<!-- 以下是手写区，重新生成不会覆盖 -->"
# 课程首页的固定 id。它是课程级的，不对应任何概念，清理孤儿页时要放过它。
INDEX_ID = "index"
# 树的最大深度，index.md 算第一层，所以概念最多用到三层。
WIKI_MAX_DEPTH = 4
# 单次构建最多写多少页：跑飞的兜底，不是目标值。教材自己的结构（书签小节数，或无书签时
# 按 MAX_EVIDENCE_CHARS 攒出的段数）在这条线以下就照它来——把作者分好的小节并成更少的页，
# 只会让每页要概括的原文成倍上涨，同样六百字留下的细节完全不是一个量级。
# 成本由构建前的预计页数与调用次数交给用户判断，不靠把这个数压小来省。
WIKI_MAX_NODES = 300
# 一页约一次模型调用。5 秒是实测均值（五份小教材 51 页共 261 秒），用来给用户一个量级。
SECONDS_PER_PAGE = 5
# 一页最多读多少字原文。超过就按分片顺序再切一层，不截断——截断就是漏。
MAX_EVIDENCE_CHARS = 6000
MAX_PAGE_BYTES = 128 * 1024
# 手写区要给下一次重建留出余量：生成区实测 1.2–1.9 KB，但换个提示词版本、换个模型都会让它变长。
# 手写区顶到 MAX_PAGE_BYTES 时下一次重写这一页必然超限，而那时用户已经花过模型的钱了。
REBUILD_HEADROOM = 8 * 1024
# 读一页的记账信息只需要开头这么多字符，整份读进来在整目录扫描时是白花的 IO。
FRONTMATTER_CHARS = 1024
# 一页知识页最多摆出每份教材的几页出处。总览页依据整本书，全列出来只会淹掉抽屉。
WIKI_SOURCE_MAX_PAGES = 8
# 出处那几页各带多长的原文，够看出「这页在讲什么」即可。
WIKI_SOURCE_SNIPPET = 200
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


@dataclass(frozen=True)
class PageSlot:
    """一页在库里的落点：祖先页对应的目录，加上它在树里的位次编号。

    文件名要等页名定下来才拼得出——没有目录的教材，段名是模型读完原文才起的。
    """
    folders: tuple[str, ...]
    number: str

    def location(self, name: str) -> tuple[str, ...]:
        return (*self.folders, f"{self.number}-{_safe(name)}")


def folder_name(filename: str, *, rank: int = 1, fallback: str = "") -> str:
    """教材在库里的目录名，取掉扩展名。同课重名的教材按名次错开，两棵树不合并进一个目录。"""
    base = _safe(Path(filename).stem) or _safe(fallback)
    return base if rank <= 1 else f"{base}-{rank}"


def page_slots(sections: list[Section], *, folder: str = "") -> dict[str, PageSlot]:
    """磁盘布局：目录层级照树层级，编号取树内位次，零填充按同级数量定宽。

    中间页的文件与它的子目录同名（Obsidian 的 folder note 惯例），整个库直接当笔记库打开就能读。
    `folder` 是这份教材自己的目录：位次是每份教材各从 1 数起的，同课几份教材摊在一层会撞号。
    """
    known = {section.id for section in sections}
    children: dict[str | None, list[Section]] = {}
    for section in sections:
        children.setdefault(section.parent_id if section.parent_id in known else None, []).append(section)
    slots: dict[str, PageSlot] = {}
    root = (folder,) if folder else ()
    queue: list[tuple[str | None, tuple[str, ...], str]] = [(None, root, "")]
    while queue:
        parent_id, folders, prefix = queue.pop(0)
        siblings = children.get(parent_id, [])
        width = len(str(len(siblings)))
        for position, section in enumerate(siblings, start=1):
            number = f"{prefix}.{position:0{width}d}" if prefix else f"{position:0{width}d}"
            slots[section.id] = PageSlot(folders, number)
            if section.children:
                queue.append((section.id, (*folders, f"{number}-{_safe(section.name)}"), number))
    return slots


class WikiStore:
    """按课程隔离的 markdown 落盘，落点校验照 NoteStore 的口径。

    磁盘上按目录树排版（`01-章名/01.1-节名.md`），当笔记库打开就能读。页的身份只认
    frontmatter 里的 concept_id，路径怎么排都不影响引用、增量刷新与手写区。
    """

    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir / "wiki"
        # concept_id → 落点，以及哪几门课已经扫过。路径可读之后按 id 找页要扫目录，
        # 扫过的结果留着，写页与搬页同步维护，所以找不到就是真没有，不必再扫一遍。
        self._located: dict[str, dict[str, Path]] = {}
        self._scanned: set[str] = set()
        # 构建在后台线程里跑，界面同时在读页：整目录扫描的「遍历 + 发布」必须和写页互斥，
        # 否则扫描会用开工前的快照盖掉刚写进去的那几页，读页拿到 LookupError。
        self._lock = threading.RLock()

    def _course_dir(self, course_id: str) -> Path:
        return self._root / _safe(course_id)

    def _path(self, *, course_id: str, concept_id: str, location: tuple[str, ...] | None = None) -> Path:
        """落点：给了 location 就按它排目录，没给就平铺成 `<id>.md`（课程首页与老版本的页）。"""
        directory = self._course_dir(course_id)
        parts = list(location or (concept_id,))
        # 目录深度封顶：教材目录 + 树深度正好用到 WIKI_MAX_DEPTH 层，兜的是 location 算错的
        # 情况（真跑飞了宁可几页挤在上一层，也不让路径无限长）。文件名那一段永远留着。
        folders = [_safe(part) for part in parts[:-1]][:WIKI_MAX_DEPTH]
        name = _safe(parts[-1])
        if not name or not all(folders) or {name, *folders} & {".", ".."}:
            raise ValueError("Wiki 页落点不合法")
        path = directory.joinpath(*folders, f"{name}.md").resolve()
        base = directory.resolve()
        if os.path.commonpath([str(base), str(path)]) != str(base):
            raise ValueError("Wiki 页只能写在本课程目录内")
        if path.is_symlink():
            raise ValueError("Wiki 页是符号链接，已拒绝写入")
        return path

    def _scan(self, course_id: str) -> list[tuple[Path, WikiPage]]:
        """遍历课程目录，认出这门课的知识页，落点一律用 resolve 后的真路径。

        只认 frontmatter 里真有 concept_id 的文件：用户自己放进库里的 markdown 不是知识页，
        列不出、也不会被清理碰到。读页与写页守同一条落点线，符号链接和跑出课程目录的都不认。
        """
        directory = self._course_dir(course_id)
        base = directory.resolve()
        found: list[tuple[Path, WikiPage]] = []
        with self._lock:
            for path in sorted(directory.rglob("*.md")) if directory.is_dir() else []:
                # 目录名也可能以 .md 结尾（小节就叫「README.md」），所以要挑出真正的文件。
                if path.is_symlink() or not path.is_file():
                    continue
                real = path.resolve()
                if os.path.commonpath([str(base), str(real)]) != str(base):
                    continue
                page = _page_of(real)
                if page.concept_id:
                    found.append((real, page))
            self._located[course_id] = {page.concept_id: path for path, page in found}
            self._scanned.add(course_id)
        return found

    def _locate(self, *, course_id: str, concept_id: str) -> Path | None:
        """按 concept_id 找落点。目录由本模块自己写，扫过一遍后靠写页与搬页增量维护，
        所以扫过之后找不到就当真没有——构建一次上百页，每次未命中都重扫等于扫上百遍。

        自愈是单向的：外部删掉或移走那份能自愈（记下的落点不在了就重扫），外部**新增**的页
        要等下一次 list_pages 才认得出（列页每次都真扫，界面与构建的每条路都先列页）。
        """
        with self._lock:
            path = self._located.get(course_id, {}).get(concept_id)
            if path is None and course_id in self._scanned:
                return None
            if path is None or not path.is_file():
                self._scan(course_id)
                path = self._located.get(course_id, {}).get(concept_id)
            return path

    def read(self, *, course_id: str, concept_id: str) -> str:
        path = self._locate(course_id=course_id, concept_id=concept_id)
        if path is None or not path.is_file():
            raise LookupError(f"没有 {concept_id} 的 Wiki 页")
        return path.read_text(encoding="utf-8")

    def source_refs(self, *, course_id: str, concept_id: str) -> list[str]:
        """读一页 frontmatter 里记的证据出处。读不到就当这页没有出处，不报错。"""
        try:
            return refs_in(self.read(course_id=course_id, concept_id=concept_id))
        except (LookupError, ValueError, OSError):
            return []

    def source_hash(self, *, course_id: str, concept_id: str) -> str:
        """读已有页的证据指纹，用来判断要不要重写。读不到就返回空串。"""
        try:
            head = self.read(course_id=course_id, concept_id=concept_id)[:800]
        except (LookupError, ValueError):
            return ""
        match = re.search(r"^source_hash:[ \t]*(\S+)$", head, re.MULTILINE)
        return match.group(1) if match else ""

    def _free_path(self, *, concept_id: str, previous: Path | None, path: Path) -> Path:
        """落点被别的页占着就加后缀。同名教材下同号同名的小节会撞在一处，撞了各自成文件，
        不能一份盖掉另一份。"""
        candidate, taken = path, 1
        while candidate != previous and candidate.is_file() and _page_of(candidate).concept_id != concept_id:
            taken += 1
            candidate = path.with_name(f"{path.stem}-{taken}.md")
        return candidate

    def relocate(self, *, course_id: str, concept_id: str, location: tuple[str, ...]) -> None:
        """把已有的页搬到它现在该在的落点。证据没变的页不重写，编号却会随目录改动而变，
        不搬的话换版之后同一目录里会并排摆着两个同号的页。目标已被别的页占着就先放着。"""
        with self._lock:
            current = self._locate(course_id=course_id, concept_id=concept_id)
            target = self._path(course_id=course_id, concept_id=concept_id, location=location)
            if current is None or current == target or not current.is_file() or target.exists():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current, target)
            self._located.setdefault(course_id, {})[concept_id] = target

    def write(self, *, course_id: str, concept_id: str, concept_name: str, body: str,
              source_hash: str, source_refs: list[str], updated_at: str,
              material_id: str = "", parent_id: str | None = None, level: int = 0,
              order: int = 0, location: tuple[str, ...] | None = None) -> WikiPage:
        with self._lock:
            previous = self._locate(course_id=course_id, concept_id=concept_id)
            path = self._free_path(concept_id=concept_id, previous=previous,
                                   path=self._path(course_id=course_id, concept_id=concept_id, location=location))
            document = self._compose(
                concept_id=concept_id, concept_name=concept_name, body=body, source_hash=source_hash,
                source_refs=source_refs, updated_at=updated_at, material_id=material_id,
                parent_id=parent_id, level=level, order=order,
                # 手写区先看旧落点（改名换号时它在那边），旧落点没有再看新落点，两头都不弄丢。
                handwritten=_handwritten_of(previous) or (_handwritten_of(path) if path != previous else ""),
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, document)
            # 改名或换编号让落点变了，旧文件要收走，否则同一页在库里留下两份。
            if previous is not None and previous != path:
                previous.unlink(missing_ok=True)
            self._located.setdefault(course_id, {})[concept_id] = path
        return WikiPage(concept_id, concept_name, source_hash, updated_at, len(document),
                        material_id, parent_id or "", level, order)

    def write_handwritten(self, *, course_id: str, concept_id: str, text: str) -> str:
        """替换分隔线以下那一段，生成区与 frontmatter 一个字节都不动。返回落盘后的整页。

        读改写整段放在锁里：构建线程同时在重写这一页时，两边不能各拿一份旧内容互相覆盖。
        """
        with self._lock:
            path = self._locate(course_id=course_id, concept_id=concept_id)
            if path is None or not path.is_file():
                raise LookupError(f"没有 {concept_id} 的 Wiki 页")
            try:
                existing = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"{concept_id} 这一页不是 UTF-8 文本，无法在界面上编辑") from error
            generated, marker, _old = existing.partition(HANDWRITTEN_MARKER)
            # 没有分隔线的老页在末尾补一条，原有内容照旧算作生成区。
            # 用户正文里写出分隔标记会让这一页出现第二条，之后读页会从错的地方切开。
            # 只删标记不删它后面的字：用户打的内容一个字都不能悄悄吞掉。
            body = text.replace(HANDWRITTEN_MARKER, "").strip()
            document = (generated if marker else existing.rstrip() + "\n\n") + HANDWRITTEN_MARKER + "\n"
            document += f"{body}\n" if body else ""
            if len(document.encode("utf-8")) > MAX_PAGE_BYTES - REBUILD_HEADROOM:
                raise WikiPageTooLargeError(
                    f"{concept_id} 这一页超过 {(MAX_PAGE_BYTES - REBUILD_HEADROOM) // 1024} KiB 上限，这次没有保存")
            write_text_atomic(path, document)
        return document

    @staticmethod
    def _compose(*, concept_id: str, concept_name: str, body: str, source_hash: str,
                 source_refs: list[str], updated_at: str, material_id: str,
                 parent_id: str | None, level: int, order: int, handwritten: str) -> str:
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
            raise WikiPageTooLargeError(f"Wiki 页 {concept_id} 超过大小上限")
        return document

    def list_pages(self, *, course_id: str) -> list[WikiPage]:
        """按构建时记下的 order 返回，首页排在最前。文件名带的编号只是排版，不拿它当顺序。"""
        pages = [page for _path, page in self._scan(course_id)]
        return sorted(pages, key=lambda page: (page.concept_id != INDEX_ID, page.order, page.concept_id))

    def prune(self, *, course_id: str, valid_concept_ids: set[str], material_id: str = "",
              planned_ids: set[str] | None = None, known_material_ids: set[str] | None = None) -> list[str]:
        """删掉不再属于这门课的页。概念表与这次的构建计划是真源。

        本次重建的那份教材按计划对账，别的教材只要还在就保留它的页；剩下的（老版本写的、
        没记教材归属的）照概念表判断。首页是课程级的，任何时候都不删。
        """
        planned = planned_ids or set()
        alive = known_material_ids or set()
        removed = []
        with self._lock:
            for path, page in self._scan(course_id):
                if page.concept_id == INDEX_ID:
                    continue
                if page.material_id and page.material_id == material_id:
                    keep = page.concept_id in planned
                elif page.material_id:
                    keep = page.material_id in alive
                else:
                    keep = page.concept_id in valid_concept_ids
                # 手写区是用户自己写的，没有第二份副本：这样的页留在原位当孤儿，也不删。
                if keep or _user_wrote_in(path):
                    continue
                path.unlink(missing_ok=True)
                removed.append(page.concept_id)
            if removed:
                self.tidy(course_id=course_id)
                self._scan(course_id)
        return removed

    def tidy(self, *, course_id: str) -> None:
        """收掉空目录，库里不留只剩名字的空章。深的先删，整条空链一次清完。"""
        directory = self._course_dir(course_id)
        if not directory.is_dir():
            return
        with self._lock:
            for path in sorted(directory.rglob("*"), reverse=True):
                if path.is_dir() and not path.is_symlink() and not any(path.iterdir()):
                    path.rmdir()

    def delete_course(self, *, course_id: str) -> None:
        """删课程时由组装根调用。目录布局是本模块自己的事，别处不该知道。"""
        with self._lock:
            shutil.rmtree(self._course_dir(course_id), ignore_errors=True)
            self._located.pop(course_id, None)
            self._scanned.discard(course_id)


def _page_of(path: Path) -> WikiPage:
    """按 frontmatter 认一页。concept_id 为空表示这个文件不是知识页，调用方据此放过它。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        head = handle.read(FRONTMATTER_CHARS)

    def field_of(key: str) -> str:
        # 空值那几行不能用 \s*：它会吃掉换行，把下一行整行当成本行的值。
        match = re.search(rf"^{key}:[ \t]*(.*)$", head, re.MULTILINE)
        return match.group(1).strip() if match else ""

    def number_of(key: str) -> int:
        # 手改坏的一行不该让整门课列不出来。能不能当数用交给 int 判，别自己写判据。
        try:
            return int(field_of(key))
        except ValueError:
            return 0

    concept_id = field_of("concept_id")
    return WikiPage(
        concept_id=concept_id, concept_name=field_of("concept_name") or concept_id,
        source_hash=field_of("source_hash"), updated_at=field_of("updated_at"), chars=path.stat().st_size,
        material_id=field_of("material_id"), parent_id=field_of("parent_id"),
        level=number_of("level"), order=number_of("order"),
    )


def _handwritten_of(path: Path | None) -> str:
    """读一页的手写区。读不到就当没有——这里的判断只用来「别弄丢」，不该反过来阻断写入。"""
    if path is None or not path.is_file():
        return ""
    try:
        existing = path.read_text(encoding="utf-8")
    except (OSError, ValueError):  # 解码错是 ValueError 的子类：按 GBK 存过的页别挂住构建
        return ""
    if HANDWRITTEN_MARKER not in existing:
        return ""
    return HANDWRITTEN_MARKER + existing.split(HANDWRITTEN_MARKER, 1)[1]


def _user_wrote_in(path: Path) -> bool:
    """分隔线之后真写了东西。每一页都带着那条分隔线，空的手写区不算。"""
    return bool(_handwritten_of(path).removeprefix(HANDWRITTEN_MARKER).strip())


# 叶子页的出处形如「math-gaussian.pdf p.10 #chunk_9a7d…」，见 _raw_evidence。
# 中间页记的是子页（「子页 X <id>」），对不上这个形状，由调用方顺着子页往下收。
_SOURCE_REF = re.compile(r"^(?P<document>.+?)(?: p\.(?P<page>\d+))? #(?P<chunk_id>\S+)$")


def refs_in(document: str) -> list[str]:
    """从落盘的一页里取出 frontmatter 记的出处行。"""
    match = re.search(r"^source_refs:\n((?:[ \t]+- .*\n)*)", document, re.MULTILINE)
    if not match:
        return []
    return [line.strip()[2:].strip() for line in match.group(1).splitlines() if line.strip().startswith("- ")]


def parse_source_refs(refs: list[str]) -> list[tuple[str, int | None, str]]:
    """把 frontmatter 的出处行拆成（文档、页码、分片 id）。拆不出教材位置的行跳过。"""
    out = []
    for ref in refs:
        if match := _SOURCE_REF.match(ref.strip()):
            page = match.group("page")
            out.append((match.group("document"), int(page) if page else None, match.group("chunk_id")))
    return out


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


def retrieval_content(document: WikiDocument) -> str:
    """这一页进检索库的正文：概念名 + 生成区，手写区非空时带身份标注一起收进来。

    用户在手写区写的纠偏要能被日常问答检索到，否则只有模型主动读这一页才看得见。
    生成区里出现的同款标注要摘掉：读的一端按第一处标注拆段，多一处就会切错地方。
    """
    parts = [f"{document.concept_name}\n\n{strip_handwritten_label(document.body)}"]
    if document.handwritten.strip():
        parts.append(f"{HANDWRITTEN_LABEL}\n{document.handwritten.strip()}")
    return "\n\n".join(parts)


def strip_handwritten_label(body: str) -> str:
    """摘掉生成区里的身份标注。全文只留一处，手写区的边界才认得准。"""
    return body.replace(HANDWRITTEN_LABEL, "").strip()


# ---- 切段：把一份教材切成一棵 Section 树 ----

def _section_id(material_id: str, key: object) -> str:
    """按教材加教材内位置派生，重建索引后同一节仍是同一个 id，增量刷新才认得出来。

    key 是这一节在教材里的位置：有目录时是名字加同名位次，没目录时是首个分片的序号。
    """
    return "section_" + hashlib.sha1(f"{material_id}\n{key}".encode()).hexdigest()[:16]


def outline_rows(*, material_id: str, candidates: list[dict]) -> list[dict]:
    """把目录候选整理成 plan_sections 要的目录行，id 按这份教材内的位置派生。

    不走 concepts 表：那张表按「课程 + 名字」给 id 并在同名时并成一行，同一门课两份教材有
    同名节时第二份那几节整节查不到，原文会被并进邻节、挂在别人的标题下面。
    id 只认「名字 + 教材内同名位次」，不含祖先路径——改一个章名不该把整棵子树连同手写区
    一起作废；代价是同名节前面再插进一处同名的，后面几处位次平移、各作废重建一次。
    与 concepts 表解耦后，掌握度按 concept_id join 到知识页的那条路不再存在（实测命中 0/5，
    日后按页码区间聚合更稳），历史会话里指向旧 concept_id 的引用点开会 404，属于「已有页
    作废重建」的一次性代价。
    """
    outline = sorted((row for row in candidates if row.get("level") is not None),
                     key=lambda row: row.get("ordinal") or 0)
    rows: list[dict] = []
    stack: list[tuple[int, str]] = []  # 祖先链：(层级, id)
    seen: dict[str, int] = {}
    for row in outline:
        level, name = int(row["level"]), str(row["name"])
        while stack and stack[-1][0] >= level:
            stack.pop()
        key = name.casefold()
        # 重名的几节（每章都有「小结」）按教材内出现位次分开，各自成页。位次与名字之间用
        # 换行分隔：书签名归一化时空白全被压成空格，换行产不出来，两段拼不出歧义。
        seen[key] = seen.get(key, 0) + 1
        page_id = _section_id(material_id, f"{seen[key]}\n{key}")
        rows.append({"id": page_id, "name": name, "page": row.get("page"), "level": level,
                     "parent_id": stack[-1][1] if stack else None})
        stack.append((level, page_id))
    return rows


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
    return sections, {"candidates": len(natural), "capped": len(natural) - len(groups)}


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
    _claim_unassigned(sections, chunks)
    # 上限砍掉的节点由上级页接过它的页码区间，所以只是「没单独成页」，不是没读到。
    return sections, {"candidates": len(outline), "capped": len(outline) - len(doc)}


def _claim_unassigned(sections: list[Section], chunks: list[dict]) -> None:
    """按页码分段会漏掉没有页码的分片（提取不出页号时就是这样），按 ordinal 就近补给叶子。

    兜底放在最后：页码区间怎么算都好，落不到任何一节的分片一律要有人读。
    """
    leaves = [section for section in sections if not section.children]
    if not leaves:
        return
    claimed = {chunk["id"] for section in sections for chunk in section.chunks}
    spans = [(min(c["ordinal"] for c in leaf.chunks), max(c["ordinal"] for c in leaf.chunks), leaf)
             for leaf in leaves if leaf.chunks]
    for chunk in chunks:
        if chunk["id"] in claimed:
            continue
        nearest = min(spans, key=lambda span: min(abs(chunk["ordinal"] - span[0]), abs(chunk["ordinal"] - span[1])),
                      default=None) if spans else None
        target = nearest[2] if nearest else leaves[0]
        target.chunks.append(chunk)
    for leaf in leaves:
        leaf.chunks.sort(key=lambda chunk: chunk["ordinal"])


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
    on_progress: Callable[[int, int], None] | None = None, folder: str = "",
) -> dict[str, int]:
    """自底向上写页，最后写课程首页。返回「写入 / 跳过 / 无内容」三个计数。"""
    counts = {"written": 0, "skipped": 0, "ungrounded": 0, "oversized": 0}
    bodies: dict[str, str] = {}
    names: dict[str, str] = {}
    positions = {section.id: index for index, section in enumerate(sections)}
    # 教材各占一个目录。目录名由调用方给（同课重名的教材要错开），没给就照文件名算。
    slots = page_slots(sections, folder=folder or folder_name(document, fallback=material_id))
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
            if slot := slots.get(section.id):
                store.relocate(course_id=course_id, concept_id=section.id,
                               location=slot.location(existing.concept_name))
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
        try:
            store.write(course_id=course_id, concept_id=section.id, concept_name=name, body=body,
                        source_hash=source_hash, source_refs=refs, updated_at=now,
                        material_id=material_id, parent_id=section.parent_id, level=section.level,
                        order=positions[section.id],
                        location=slot.location(name) if (slot := slots.get(section.id)) else None)
        except WikiPageTooLargeError:
            # 一页落不了盘（手写区把整页撑满了）不该拖垮整次构建：后面的页照写，
            # 这一页保持盘上的旧版本，条数如实报出来让用户知道有一页没更新。
            counts["oversized"] += 1
            continue
        bodies[section.id], names[section.id] = body, name
        counts["written"] += 1

    if on_progress is not None:
        on_progress(total, total)
    _write_index(course_id=course_id, store=store, now=now, ask=ask, counts=counts)
    # 改名换号会把页搬走，原来那一层可能空了。
    store.tidy(course_id=course_id)
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
    try:
        store.write(course_id=course_id, concept_id=INDEX_ID, concept_name="课程总览",
                    body=f"{body}\n\n{directory}", source_hash=source_hash,
                    source_refs=[f"顶层页 {page.concept_name}" for page in pages if page.level == 0],
                    updated_at=now, level=0, order=-1)
    except WikiPageTooLargeError:
        counts["oversized"] += 1
        return
    counts["written"] += 1


# ---- 体检：零模型调用的确定性检查，只报不改 ----

# 正文里的出处标注，三种形态与前端的 CITE_MARK 同一口径：[文档 p.12]、[p.12]、[笔记.docx]。
_CITE_MARK = re.compile(r"\[(?:([^\]\n]+) )?p\.(\d+)\]|\[([^\]\n]+\.(?:pdf|docx?|pptx?|txt|md))\]", re.I)
# 文档名那一半得真像个文件名才拿去比对。切歪的（「讲义.pdf p.1,」）与泛指（「第三章」）都判不出结论。
_HAS_SUFFIX = re.compile(r"\.(?:pdf|docx?|pptx?|txt|md)$", re.I)
# 代码里的写法不算标注：前端也不给 pre/code 接原文。围栏、行内、四空格缩进三种都要认，
# 缩进那条放过列表项——缩进的列表在前端渲染成 li，标注照样是可点的。
_CODE_SPAN = re.compile(r"(?s:```.*?```)|`[^`\n]*`|(?m:^(?: {4}|\t)(?![-*+] |\d+\. ).*$)")
# 带页码的标注按这个键与本页 refs 配对；文档级标注的页码位是 None。
_PAGE_CODES = {"page_out_of_range", "overview_cites_pages"}
# 一条发现里最多列几个页码或文件名。真正的条数由 n 说明，整表摆出来会淹掉界面。
LINT_SAMPLE_MAX = 12


@dataclass(frozen=True)
class LintPage:
    """体检要看的一页：正文（不含手写区）、frontmatter 的出处行，加几个记账字段。"""
    concept_id: str
    concept_name: str
    body: str
    refs: tuple[str, ...] = ()
    parent_id: str = ""
    material_id: str = ""
    source_hash: str = ""


def _loose_name(document: str) -> str:
    """文档名的松匹配口径，与前端 anchorLookup 一致：大小写、空白、开头的 p. 与扩展名都不计。"""
    name = re.sub(r"\s+", " ", document).strip().casefold().removeprefix("p.")
    return re.sub(r"\.[a-z0-9]+$", "", name)


def _marks_in(body: str) -> list[tuple[str, int | None]]:
    """正文里的出处标注，逐个拆成（文档名, 页码）。这两半都可能缺，缺的那半是空串或 None。"""
    text = _CODE_SPAN.sub(" ", body)
    return [(match.group(1) or match.group(3) or "",
             int(match.group(2)) if match.group(2) else None)
            for match in _CITE_MARK.finditer(text)]


def _classify_mark(document: str, number: int | None, *, overview: bool, cited: set[tuple[str, int | None]],
                   cited_pages: set[int], cited_names: set[str], known_names: set[str],
                   ) -> tuple[str, str, object] | None:
    """一条出处标注落到哪条规则上，落不到就返回 None。返回（code, level, 报出来的值）。

    `cited` 是本页出处的（文档, 页）对，`cited_pages` 与 `cited_names` 是它的两个投影。
    """
    # 中间页与首页读的是子页不是原文，页码无从核对，提示词也禁止标——标了就是编的。
    if overview and number is not None:
        return "overview_cites_pages", "error", number
    if not document:
        return None if number in cited_pages else ("page_out_of_range", "error", number)
    if not _HAS_SUFFIX.search(document):
        return None
    name = _loose_name(document)
    # 文档级标注（没有页码）只要这本书读过就算对上，不必逐页配。
    if (name, number) in cited or (number is None and name in cited_names):
        return None
    if name in cited_names:
        return "page_out_of_range", "error", number
    # 这门课确实有这份教材，只是这一页没读过它。可能是对的，交给用户看，不当编造。
    if name in known_names:
        return "cross_document_mark", "warn", document
    # 整份文档标注常常只是行文里提了个文件名（「配置写在 README.md 里」），比编造页码轻一档。
    return "fabricated_document", "error" if number is not None else "warn", document


def lint_pages(pages: list[LintPage], *, material_pages: dict[str, set[int]],
               material_names: dict[str, str]) -> list[dict[str, object]]:
    """给一门课的知识页做体检，只报不改。error 是该重建或该修的，warn 是提示。

    `material_pages` 是每份教材在检索库里实际存在的页码，`material_names` 是教材 id 到文件名。
    两者由调用方查库给出，这里不碰 IO——每条规则都要能用手造的页数据钉住。

    每条发现都带 concept_id 与 concept_name；教材级的对账落不到某一页，concept_id 是空串、
    名字位上是教材文件名。`code` 决定文案，其余键是文案的插值参数。
    """
    issues: list[dict[str, object]] = []

    def report(page: LintPage, level: str, code: str, **params: object) -> None:
        issues.append({"concept_id": page.concept_id, "concept_name": page.concept_name,
                       "level": level, "code": code, **params})

    known_ids = {page.concept_id for page in pages}
    parents = {page.parent_id for page in pages if page.parent_id}
    known_names = {_loose_name(name) for name in material_names.values()}
    read_pages: dict[str, set[int]] = {}

    for page in pages:
        located = parse_source_refs(list(page.refs))
        cited_pages = {number for _document, number, _chunk in located if number is not None}
        cited = {(_loose_name(document), number) for document, number, _chunk in located}
        cited_names = {name for name, _number in cited}
        if page.material_id:
            read_pages.setdefault(page.material_id, set()).update(cited_pages)
        overview = page.concept_id == INDEX_ID or page.concept_id in parents
        # 一条标注只落一条规则，先判最能说明问题的那条：同一处写法报两遍会让报告没人看。
        buckets: dict[tuple[str, str], list] = {}
        for document, number in _marks_in(page.body):
            if verdict := _classify_mark(document, number, overview=overview, cited=cited,
                                         cited_pages=cited_pages, cited_names=cited_names,
                                         known_names=known_names):
                buckets.setdefault(verdict[:2], []).append(verdict[2])
        for (code, level), values in sorted(buckets.items(), key=lambda item: (item[0][1] != "error", item[0][0])):
            unique = sorted(set(values))
            report(page, level, code, n=len(unique),
                   **{"pages" if code in _PAGE_CODES else "documents": _sample(unique)})
        if not overview and not located:
            report(page, "error", "leaf_without_sources")
        if not page.body.strip():
            report(page, "warn", "empty_body")
        if page.parent_id and page.parent_id not in known_ids:
            report(page, "warn", "orphan_page", parent=page.parent_id)
        if not page.source_hash:
            report(page, "warn", "no_source_hash")

    issues += _coverage_issues(read_pages, material_pages, material_names)
    # error 先摆出来，同级内保持页面顺序。
    return sorted(issues, key=lambda issue: issue["level"] != "error")


def _coverage_issues(read_pages: dict[str, set[int]], material_pages: dict[str, set[int]],
                     material_names: dict[str, str]) -> list[dict[str, object]]:
    """出处与检索库的页码对账，两边都来自实际内容，正常构建应当吻合。

    只对账有知识页的教材：没生成过页的教材两边必然对不上，那不是缺陷。
    「没人读的页」降为 warn——上级页接管区间、空白页不进检索库这些形态都可能让它响。
    """
    out: list[dict[str, object]] = []
    for material_id, seen in sorted(read_pages.items()):
        indexed = material_pages.get(material_id)
        if indexed is None:  # 这份教材的正文一页页码都没有，对不出结论
            continue
        # 对账是教材级的，落不到某一页上：concept_id 留空，名字位上给教材文件名。
        row = {"concept_id": "", "concept_name": material_names.get(material_id, material_id)}
        if dangling := sorted(seen - indexed):
            out.append({**row, "level": "error", "code": "dangling_page_refs",
                        "pages": _sample(dangling), "n": len(dangling)})
        if unread := sorted(indexed - seen):
            out.append({**row, "level": "warn", "code": "unread_pages",
                        "pages": _sample(unread), "n": len(unread)})
    return out


def _sample(values: list) -> list:
    return values[:LINT_SAMPLE_MAX]


def coverage_summary(counts: dict[str, object]) -> str:
    """构建结果的机器可读汇总，界面按字段渲染中英两版。

    覆盖率必须说出来：节点上限之下写出的仍然是这本书的一部分，静默截断读起来像写全了。
    `outline` 报目录是从教材现算的还是退回了概念表，降质同样不该静默。
    体检没跑成时 `issues` 字段不出现，不写 0——0 是「查过、没问题」的结论，不能拿来顶替。
    `oversized` 是落不了盘的页（手写区把整页撑满了），它们保持旧版本，和 `empty` 不是一回事。
    """
    def count(key: str) -> int:
        value = counts.get(key, 0)
        return value if isinstance(value, int) else 0

    fields = [f"concepts={count('candidates')}",
              f"pages={count('written') + count('skipped')}",
              f"written={count('written')}", f"skipped={count('skipped')}",
              f"merged={count('capped')}",
              f"empty={count('ungrounded')}", f"oversized={count('oversized')}",
              f"pruned={count('pruned')}"]
    if "issues" in counts:
        fields.append(f"issues={count('issues')}")
    fields.append(f"outline={counts.get('outline', 'material')}")
    return "wiki_coverage " + " ".join(fields)


# ---- 配对：哪几个来源在讲同一件事。读时现算，不落盘 ----

# 每页取几个最近邻当候选。只连互为近邻的一对，k 再放大也只是让相似的页连成一团。
WIKI_PAIR_K = 6
# 一页最多连几条边。这一行是读页时的旁注，不是目录，多了会盖过正文。
WIKI_PAIR_MAX = 3


@dataclass(frozen=True)
class PairNode:
    """参与配对的一页：教材归属定连不连，直系父页用来标定门槛。"""
    concept_id: str
    material_id: str
    parent_id: str


def pair_pages(nodes: list[PairNode], similarity: list[list[float]]) -> list[dict[str, object]]:
    """哪几个来源在讲同一件事。输入是页的归属与它们两两的相似度，这里不碰 IO 也不碰向量。

    **只连跨教材的页。** 一门课只有一本书时本来就没有「几个来源」，同一本书里的相邻小节是
    「相关」不是「同一件事」，它们的关系中间页也已经用自然语言写过。同章的两页必然同教材，
    所以这一条把同章的边一并挡在外面，不必再看树。
    连边还要互为近邻、余弦为正、分数过这门课自己标定的门槛；一条都没连上的页留最像的那个来源。
    """
    # 矩阵不是 n×n 就是调用方接错了。这里不抛：配对是页面上的旁注，坏数据只该让它不显示。
    if len(nodes) < 2 or len(similarity) != len(nodes) or any(len(row) != len(nodes) for row in similarity):
        return []
    # 余弦不为正的两页，在任何标定口径下都不可能是「在讲同一件事」。门槛是相对量（页对的中位数），
    # 一门课的页彼此都不像时它会一路降到 0，这条是它下面唯一的绝对线，主路径与保底都受它管。
    candidates = [pair for pair in _mutual_pairs(similarity)
                  if pair[2] > 0 and nodes[pair[0]].material_id != nodes[pair[1]].material_id]
    threshold = _sibling_threshold(nodes, similarity)
    degree: dict[int, int] = {}
    edges = _take_edges([pair for pair in candidates if pair[2] >= threshold], degree)
    edges += _lonely_page_floor(candidates, degree)
    return [{"a": nodes[left].concept_id, "b": nodes[right].concept_id, "score": round(score, 4)}
            for left, right, score in sorted(edges, key=lambda edge: (-edge[2], edge[0], edge[1]))]


def _mutual_pairs(similarity: list[list[float]]) -> list[tuple[int, int, float]]:
    """互为 k 近邻的页对。单向不算：写得概括的那一页谁都觉得像，它会挂满整门课。"""
    count = len(similarity)
    nearest = [set(sorted((other for other in range(count) if other != index),
                          key=lambda other: (-similarity[index][other], other))[:WIKI_PAIR_K])
               for index in range(count)]
    return [(left, right, similarity[left][right])
            for left in range(count) for right in sorted(nearest[left])
            if right > left and left in nearest[right]]


def _sibling_threshold(nodes: list[PairNode], similarity: list[list[float]]) -> float:
    """门槛每门课自己标定：余弦的绝对值不跨库可比（rag_min_similarity 默认关掉正是这个原因）。

    同一个直系父页下的兄弟天生在讲相近的东西，取它们两两相似度的中位数当「讲的是同一件事」的下限。
    顶层页不算一组：它们分属各本教材，凑在一起标出来的数代表不了「同一章里的两页有多像」。
    一对兄弟都没有的课（页全在顶层）退回全部页对的中位数。
    """
    groups: dict[str, list[int]] = {}
    for index, node in enumerate(nodes):
        if node.parent_id:
            groups.setdefault(node.parent_id, []).append(index)
    scores = [similarity[left][right] for members in groups.values() for left, right in combinations(members, 2)]
    if not scores:
        scores = [similarity[left][right] for left, right in combinations(range(len(nodes)), 2)]
    return median(scores) if scores else 0.0


def _take_edges(pairs: list[tuple[int, int, float]], degree: dict[int, int]) -> list[tuple[int, int, float]]:
    """分数高的先占位，两端都没到上限才收。同分按下标定序，同一份数据每次给同样的结果。"""
    kept = []
    for left, right, score in sorted(pairs, key=lambda pair: (-pair[2], pair[0], pair[1])):
        if degree.get(left, 0) < WIKI_PAIR_MAX and degree.get(right, 0) < WIKI_PAIR_MAX:
            degree[left] = degree.get(left, 0) + 1
            degree[right] = degree.get(right, 0) + 1
            kept.append((left, right, score))
    return kept


def _lonely_page_floor(candidates: list[tuple[int, int, float]],
                       degree: dict[int, int]) -> list[tuple[int, int, float]]:
    """一条边都没连上的页，把它最像的那个来源留下来（候选到这里已经全是跨教材的）。

    门槛按同章兄弟标定，而另一本书讲同一节时用的是另一套措辞，未必够得着那条线；
    「几本书都讲了这一节」正是这件功能要兑付的东西，不该被它挡掉。
    """
    rescued: list[tuple[int, int, float]] = []
    for index in sorted({end for pair in candidates for end in pair[:2]}):
        if degree.get(index, 0):
            continue
        outside = [pair for pair in candidates if index in pair[:2]
                   and degree.get(pair[1] if pair[0] == index else pair[0], 0) < WIKI_PAIR_MAX]
        if outside:
            rescued += _take_edges([max(outside, key=lambda pair: (pair[2], -pair[0], -pair[1]))], degree)
    return rescued
