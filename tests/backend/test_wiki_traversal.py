"""知识页按教材目录自底向上全量遍历，不再靠检索取几段证据。

判据是「不漏」：每一页原文都要有某一页知识页读到过，一个横跨多节讲的概念，
它那棵子树要覆盖到全部讲到它的页。检索路径做不到这件事——一次召回就那么几条。
"""
from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from contracts.llm import ChatFinal
from core.settings import Settings
from core.store import SQLiteStore
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.concepts import concept_id_for
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.wiki import WikiStore
from modules.knowledge.worker import KnowledgeJobWorker

FIXTURES = Path(__file__).resolve().parents[2] / "testdata" / "fixtures"
DEEP_LEARNING = FIXTURES / "深度学习-批量规范化.pdf"
NO_OUTLINE = FIXTURES / "os-cpu-scheduling.pdf"
DEEP_LEARNING_PAGES = 10
NO_OUTLINE_PAGES = 13

needs_deep_learning = pytest.mark.skipif(
    not DEEP_LEARNING.exists(), reason=f"缺少切片教材 {DEEP_LEARNING.name}（scripts/e2e_fixture.py 生成）")
needs_no_outline = pytest.mark.skipif(
    not NO_OUTLINE.exists(), reason=f"缺少切片教材 {NO_OUTLINE.name}（scripts/e2e_fixture.py 生成）")


class RecordingResponder:
    """记下每次调用看到的提示词。正文里带一个页码引用，证明「叶子页会标引用」这条路没断。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, *, messages, tools=()):
        self.prompts.append(messages[-1].content)
        yield ChatFinal(text="标题：本段小节\n\n这一段按给定内容写成的正文。", finish_reason="stop",
                        provider="stub", model="stub", mode="stub")


@dataclass
class Built:
    course_id: str
    material_id: str
    service: KnowledgeService
    store: WikiStore
    responder: RecordingResponder
    job: object


def _env(tmp_path):
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=600, chunk_overlap=120, top_k_results=6,
        material_max_bytes=20 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    course = CourseService(CourseRepository(store)).create_course(name="测试课")
    responder = RecordingResponder()
    wiki_store = WikiStore(settings.data_dir)
    service = KnowledgeService(
        repository=KnowledgeRepository(store), settings=settings,
        wiki_is_enabled=lambda _course_id: True, wiki_store=wiki_store, responder=responder,
    )
    worker = KnowledgeJobWorker(service, workers=1, queue_capacity=4)
    worker.start()
    return course, service, worker, wiki_store, responder


def _index_and_build(service, worker, *, course_id: str, filename: str, mime_type: str, content: bytes):
    material = service.upload_material(course_id=course_id, filename=filename,
                                       mime_type=mime_type, content=content)
    _wait(service, worker, service.enqueue_index(material_id=material.id).id)
    job = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    assert job.status == "completed", job.error_message
    return material, job


def _build(tmp_path, pdf: Path) -> Built:
    course, service, worker, wiki_store, responder = _env(tmp_path)
    try:
        material, job = _index_and_build(service, worker, course_id=course.id, filename=pdf.name,
                                         mime_type="application/pdf", content=pdf.read_bytes())
    finally:
        worker.shutdown()
    return Built(course.id, material.id, service, wiki_store, responder, job)


def _wait(service, worker, job_id: str):
    assert worker.submit(job_id)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = service.get_job(job_id=job_id)
        if job and job.status in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    pytest.fail("任务没有进入终态")


def _frontmatter(raw: str) -> dict[str, str]:
    head = raw.split("\n---\n", 1)[0]
    return {match.group(1): match.group(2).strip()
            for match in re.finditer(r"^([a-z_]+):\s*(.*)$", head, re.MULTILINE)}


def _refs(raw: str) -> list[str]:
    block = raw.split("source_refs:", 1)[1].split("\n---\n", 1)[0] if "source_refs:" in raw else ""
    return [line.strip()[2:] for line in block.splitlines() if line.strip().startswith("- ")]


def _pages_in(text: str) -> set[int]:
    return {int(number) for number in re.findall(r"p\.(\d+)", text)}


def _all_pages(built: Built) -> dict[str, str]:
    return {page.concept_id: built.store.read(course_id=built.course_id, concept_id=page.concept_id)
            for page in built.store.list_pages(course_id=built.course_id)}


def _chunk_ids(built: Built) -> set[str]:
    return {chunk["id"] for chunk
            in built.service._repository.list_material_chunks(material_id=built.material_id)}


def _read_material(built: Built) -> tuple[set[int], set[str]]:
    """所有知识页的出处加起来，读到了教材的哪几页、哪几个分片。"""
    pages: set[int] = set()
    chunks: set[str] = set()
    for raw in _all_pages(built).values():
        for ref in _refs(raw):
            pages |= _pages_in(ref)
            if match := re.search(r"#(\S+)", ref):
                chunks.add(match.group(1))
    return pages, chunks


def _covered_pages(documents: dict[str, str], concept_id: str) -> set[int]:
    """一页知识页连同它的子页一共读到了教材的哪几页。"""
    pages = _pages_in("\n".join(_refs(documents.get(concept_id, ""))))
    for other_id, raw in documents.items():
        if _frontmatter(raw).get("parent_id") == concept_id:
            pages |= _covered_pages(documents, other_id)
    return pages


# ---- 「不漏」：一个跨多节讲的概念，它那棵子树要覆盖全部讲到它的页 ----

@needs_deep_learning
def test_a_concept_taught_across_many_sections_covers_all_of_them(tmp_path):
    """批量规范化在 p.1-p.8 反复讲（引入、层、从零实现、LeNet、简明实现、争议）。
    按概念名检索只看得到召回的那几条，全量遍历看得到整棵子树。"""
    built = _build(tmp_path, DEEP_LEARNING)
    documents = _all_pages(built)
    concept_id = concept_id_for(built.course_id, "批量规范化")

    assert concept_id in documents, "这门课最主要的概念必须有页"
    covered = _covered_pages(documents, concept_id)
    assert covered >= set(range(1, 9)), f"只覆盖到 {sorted(covered)}"


@needs_deep_learning
def test_every_page_of_the_material_is_read_by_some_page(tmp_path):
    """切段不漏页：教材的每一页、每一个分片都要被某一页知识页读到。

    只看页码不够——检索路径凑巧也能碰到每一页，漏的是页里的分片。
    """
    built = _build(tmp_path, DEEP_LEARNING)
    covered_pages, covered_chunks = _read_material(built)

    assert not set(range(1, DEEP_LEARNING_PAGES + 1)) - covered_pages
    assert covered_chunks == _chunk_ids(built), \
        f"只读到 {len(covered_chunks & _chunk_ids(built))}/{len(_chunk_ids(built))} 个分片"


@needs_deep_learning
def test_leaf_ranges_tile_the_whole_material(tmp_path):
    """按目录切出来的叶子区间并起来必须盖住 1..10，一页都不少。"""
    from modules.knowledge.wiki import plan_sections

    built = _build(tmp_path, DEEP_LEARNING)
    sections, _stats = plan_sections(
        material_id=built.material_id,
        concepts=built.service._repository.list_material_concept_tree(material_id=built.material_id),
        chunks=built.service._repository.list_material_chunks(material_id=built.material_id),
    )
    assert _leaf_pages(sections) == set(range(1, DEEP_LEARNING_PAGES + 1))


@needs_deep_learning
def test_intermediate_pages_read_child_pages_not_raw_text(tmp_path):
    """中间页只读子页正文。它的出处应当指向子页，而不是教材页码。"""
    built = _build(tmp_path, DEEP_LEARNING)
    documents = _all_pages(built)
    parents = {_frontmatter(raw).get("parent_id") for raw in documents.values()} - {None, ""}
    assert parents, "应当生成出带子页的中间页"

    for concept_id in parents:
        raw = documents.get(concept_id)
        if raw is None:
            continue
        refs = _refs(raw)
        assert refs, f"{concept_id} 没有记出处"
        assert not _pages_in("\n".join(refs)), f"中间页 {concept_id} 的出处里出现了教材页码：{refs}"


@needs_deep_learning
def test_index_page_lists_the_whole_course(tmp_path):
    built = _build(tmp_path, DEEP_LEARNING)
    documents = _all_pages(built)

    assert "index" in documents, "应当生成课程首页 index.md"
    body = documents["index"]
    for name in ("批量规范化", "残差网络"):
        assert name in body, f"首页目录里少了 {name}"


@needs_deep_learning
def test_the_build_reports_its_coverage(tmp_path):
    """限制了覆盖就要说出来：静默截断读起来像覆盖了全部。"""
    built = _build(tmp_path, DEEP_LEARNING)
    summary = built.job.error_message or ""

    assert summary.startswith("wiki_coverage "), summary
    fields = dict(item.split("=", 1) for item in summary.split()[1:])
    assert int(fields["concepts"]) >= 13
    assert int(fields["pages"]) >= 10
    assert "merged" in fields and "skipped" in fields


# ---- 无书签教材走同一条流程 ----

@needs_no_outline
def test_a_material_without_bookmarks_is_still_walked_end_to_end(tmp_path):
    built = _build(tmp_path, NO_OUTLINE)
    assert len(_all_pages(built)) >= 3, "没有书签也要切段生成页"

    covered_pages, covered_chunks = _read_material(built)
    assert not set(range(1, NO_OUTLINE_PAGES + 1)) - covered_pages
    assert covered_chunks == _chunk_ids(built), \
        f"只读到 {len(covered_chunks & _chunk_ids(built))}/{len(_chunk_ids(built))} 个分片"


def test_building_one_material_keeps_the_other_materials_pages(tmp_path):
    """一门课两份教材：给第二份建页不能把第一份的页当孤儿清掉。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        _index_and_build(service, worker, course_id=course.id, filename="第一份.md",
                         mime_type="text/markdown", content="# 极限\n\n极限描述趋势。\n".encode())
        first = {page.concept_id for page in store.list_pages(course_id=course.id)}
        _index_and_build(service, worker, course_id=course.id, filename="第二份.md",
                         mime_type="text/markdown", content="# 连续性\n\n连续建立在极限之上。\n".encode())
    finally:
        worker.shutdown()

    after = {page.concept_id for page in store.list_pages(course_id=course.id)}
    assert first <= after, f"第一份教材的页被清掉了：{sorted(first - after)}"
    assert len(after) > len(first), "第二份教材应当写出新的页"


# ---- 纯函数：页码切段的三种边界与节点上限 ----

def _concepts(rows: list[tuple[str, int | None, int, str | None]]) -> list[dict]:
    return [{"id": name, "name": name, "page": page, "level": level, "parent_id": parent}
            for name, page, level, parent in rows]


def _chunks(pages: list[int], *, size: int = 100) -> list[dict]:
    return [{"id": f"chunk_{index}", "ordinal": index, "page": page, "content": f"第 {page} 页正文" + "x" * size}
            for index, page in enumerate(pages)]


def _plan(concepts, chunks, **kwargs):
    from modules.knowledge.wiki import plan_sections

    return plan_sections(material_id="m1", concepts=concepts, chunks=chunks, **kwargs)


def _leaf_pages(sections) -> set[int]:
    covered: set[int] = set()
    parents = {section.parent_id for section in sections}
    for section in sections:
        if section.id in parents:
            continue
        covered |= set(range(section.first_page, section.last_page + 1))
    return covered


def test_siblings_that_start_on_one_page_do_not_collapse_to_an_empty_range():
    """实测：批量规范化那份 p.3 上有三个二级小节，区间不能退化成空。"""
    sections, _stats = _plan(
        _concepts([("章", 1, 0, None), ("甲", 3, 1, "章"), ("乙", 3, 1, "章"), ("丙", 3, 1, "章")]),
        _chunks([1, 2, 3, 4]),
    )
    by_name = {section.name: section for section in sections}
    for name in ("甲", "乙", "丙"):
        section = by_name[name]
        assert section.last_page >= section.first_page, f"{name} 的区间退化了"
        assert section.chunks, f"{name} 一段原文都没分到"
    # 挨着的两个只差顺序，起点不能被前一个吃掉
    assert by_name["乙"].first_page == by_name["丙"].first_page == 3


def test_the_last_section_runs_to_the_end_of_the_material():
    """最后一个节点没有下界，拿教材总页数兜底，否则尾巴几页谁都不读。"""
    sections, _stats = _plan(
        _concepts([("前", 1, 0, None), ("后", 3, 0, None)]),
        _chunks([1, 2, 3, 4, 5, 6]),
    )
    assert _leaf_pages(sections) == {1, 2, 3, 4, 5, 6}


def test_ranges_overlap_by_a_page_so_a_cross_page_title_loses_nothing():
    """书签页码指的是标题所在页，跨页标题会指到上一页：边界那页两边都读，不留缝。"""
    sections, _stats = _plan(
        _concepts([("前", 1, 0, None), ("后", 4, 0, None)]),
        _chunks([1, 2, 3, 4]),
    )
    by_name = {section.name: section for section in sections}
    assert by_name["前"].last_page == 4 and by_name["后"].first_page == 4


def test_a_parent_intro_before_its_first_child_is_not_dropped():
    sections, _stats = _plan(
        _concepts([("章", 2, 0, None), ("节", 5, 1, "章")]),
        _chunks([1, 2, 3, 4, 5, 6]),
    )
    assert _leaf_pages(sections) == {1, 2, 3, 4, 5, 6}


def test_the_node_cap_drops_the_deepest_pages_and_reports_how_many():
    rows = [("章", 1, 0, None)] + [(f"节{index}", index + 1, 1, "章") for index in range(1, 12)]
    sections, stats = _plan(_concepts(rows), _chunks(list(range(1, 14))), max_nodes=5)

    assert len(sections) <= 5
    assert stats["capped"] == len(rows) - len(sections)
    # 砍掉的深层节点由上级页兜住，覆盖不能因为上限出现空洞
    assert _leaf_pages(sections) == set(range(1, 14))


def test_a_material_without_an_outline_is_sliced_by_chunk_order():
    sections, stats = _plan([], _chunks(list(range(1, 11)), size=3000))

    assert len(sections) >= 2, "一段读不完就该切开"
    assert all(section.name == "" for section in sections), "段名留给模型读完自己起"
    assert _leaf_pages(sections) == set(range(1, 11))
    assert stats["dropped"] == 0


def test_a_section_too_long_to_read_in_one_go_is_split_further():
    """目录太粗时一节可能有几十页。再切一层，不截断——截断就是漏。"""
    sections, _stats = _plan(
        _concepts([("整章", 1, 0, None)]),
        _chunks(list(range(1, 11)), size=3000),
    )
    parents = {section.parent_id for section in sections} - {None}
    assert parents == {"整章"}, "超长的一节应当长出子段"
    assert all(section.name == "" for section in sections if section.parent_id), "子段的名字留给模型"
    assert _leaf_pages(sections) == set(range(1, 11))


def test_segments_beyond_the_cap_are_reported_as_dropped():
    """没有目录时上限之外的段没有上级页兜住，是真的没读到，必须单独报出来。"""
    _sections, stats = _plan([], _chunks(list(range(1, 21)), size=5000), max_nodes=4)

    assert stats["candidates"] > 4 and stats["dropped"] == stats["candidates"] - 4
