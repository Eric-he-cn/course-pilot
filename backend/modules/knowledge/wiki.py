"""Course Wiki：把教材里的概念写成一页页可浏览的知识页。

和每轮临时检索的区别是它把理解沉淀下来（架构 §8.1/§8.2 的定位）。三条硬约束：

1. **只用检索到的原文**。写不出来就标「教材未覆盖」，不让模型拿通用知识补。
2. **增量刷新**。证据没变就不重写，省 token 也省得每次生成一个不一样的版本。
3. **手写区不动**。分隔线以下是用户自己写的，重新生成只换上半部分。
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from contracts.knowledge import ConceptRef, KnowledgeHit, WikiDocument
from contracts.llm import ChatFinal, ChatMessage

PROMPT_VERSION = "wiki-v1"
# 分隔线以下归用户。重新生成只替换上半部分，手写内容不会被冲掉。
HANDWRITTEN_MARKER = "<!-- 以下是手写区，重新生成不会覆盖 -->"
MAX_EVIDENCE_CHARS = 6000
MAX_PAGE_BYTES = 128 * 1024
_ALLOWED = re.compile(r"[^\w一-鿿\-_.]", re.UNICODE)

_SYSTEM = """你在为一门课的知识库写一页概念说明，读者是正在学这门课的学生。

只依据下面给出的教材片段来写。规则：
- 每个结论后面标出出处。每段片段的开头都给了它的标签（形如【p.12】或【笔记.docx】），
  照抄那个标签放进方括号里，例如 [p.12]、[笔记.docx]。不要自己编 [p.未标页] 这类写法。
- 教材片段没有覆盖到的内容，**整条不要写**。宁可少写一条，也不要写「教材未覆盖」之后再
  用你自己的知识补一句——那样读者分不清哪句有出处。确实需要提醒读者教材没讲到的地方时，
  只写一句「教材未覆盖」并就此收住。
- 不要复述整段原文，用自己的话把概念讲清楚。
- 用 markdown。结构：一句话定义 → 关键点（3-6 条）→ 常见误解或易错点（有就写，没有就省略）。
- 不要写标题行（# 概念名），调用方会加。
- 全文控制在 500 字以内。"""


@dataclass(frozen=True)
class WikiPage:
    concept_id: str
    concept_name: str
    source_hash: str
    updated_at: str
    chars: int


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
            head = self.read(course_id=course_id, concept_id=concept_id)[:600]
        except (LookupError, ValueError):
            return ""
        match = re.search(r"^source_hash:\s*(\S+)$", head, re.MULTILINE)
        return match.group(1) if match else ""

    def write(self, *, course_id: str, concept_id: str, concept_name: str, body: str,
              source_hash: str, source_refs: list[str], updated_at: str) -> WikiPage:
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
            f"source_hash: {source_hash}\nprompt_version: {PROMPT_VERSION}\n"
            f"updated_at: {updated_at}\nsource_refs:\n{refs}\n---\n\n"
            f"# {concept_name}\n\n{body.strip()}\n\n{handwritten or HANDWRITTEN_MARKER + chr(10)}"
        )
        if len(document.encode("utf-8")) > MAX_PAGE_BYTES:
            raise ValueError("Wiki 页超过大小上限")
        path.write_text(document, encoding="utf-8")
        return WikiPage(concept_id, concept_name, source_hash, updated_at, len(document))

    def list_pages(self, *, course_id: str) -> list[WikiPage]:
        directory = self._course_dir(course_id)
        pages: list[WikiPage] = []
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            head = path.read_text(encoding="utf-8")[:600]

            def field(key: str, text: str = head) -> str:
                match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
                return match.group(1).strip() if match else ""

            pages.append(WikiPage(
                concept_id=field("concept_id") or path.stem, concept_name=field("concept_name") or path.stem,
                source_hash=field("source_hash"), updated_at=field("updated_at"), chars=path.stat().st_size,
            ))
        return pages

    def prune(self, *, course_id: str, valid_concept_ids: set[str]) -> list[str]:
        """删掉概念已经不存在的页。

        重建索引会换掉概念列表（比如从刮标题改成读目录书签），旧概念的页文件不会自己消失，
        于是知识页里混着一堆已经不存在的概念——看上去就是这个功能坏了。概念表是真源。
        """
        directory = self._course_dir(course_id)
        removed = []
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            if path.stem not in valid_concept_ids:
                path.unlink(missing_ok=True)
                removed.append(path.stem)
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


def _evidence(hits: Iterable[KnowledgeHit]) -> tuple[str, list[str], str]:
    """把检索到的片段拼成证据块，同时算出指纹与出处清单。"""
    blocks, refs, total = [], [], 0
    for hit in hits:
        citation = hit.citation
        where = citation.document + (f" p.{citation.page}" if citation.page else "")
        text = hit.content.strip()
        if total + len(text) > MAX_EVIDENCE_CHARS:
            break
        total += len(text)
        blocks.append(f"【{where}】\n{text}")
        refs.append(f"{where} #{citation.chunk_id}")
    body = "\n\n".join(blocks)
    return body, refs, hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


def build_pages(
    *, course_id: str, concepts: list[ConceptRef], store: WikiStore, now: str,
    search: Callable[[str], list[KnowledgeHit]],
    ask: Callable[[list[ChatMessage]], ChatFinal],
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """逐个概念生成页面。返回「写入 / 跳过 / 无证据」三个计数。"""
    counts = {"written": 0, "skipped": 0, "ungrounded": 0}
    for index, concept in enumerate(concepts, start=1):
        if on_progress is not None:
            on_progress(index, len(concepts))
        hits = search(concept.name)
        if not hits:
            # 检索不到证据就不写这一页。宁可少一页，也不要一页凭空写的东西。
            counts["ungrounded"] += 1
            continue
        evidence, refs, source_hash = _evidence(hits)
        if store.source_hash(course_id=course_id, concept_id=concept.id) == source_hash:
            counts["skipped"] += 1
            continue
        final = ask([
            ChatMessage(role="system", content=_SYSTEM),
            ChatMessage(role="user", content=f"概念：{concept.name}\n\n教材片段：\n\n{evidence}"),
        ])
        body = (final.text or "").strip()
        if not body:
            counts["ungrounded"] += 1
            continue
        store.write(
            course_id=course_id, concept_id=concept.id, concept_name=concept.name,
            body=body, source_hash=source_hash, source_refs=refs, updated_at=now,
        )
        counts["written"] += 1
    return counts
