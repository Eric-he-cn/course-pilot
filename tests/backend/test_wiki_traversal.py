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
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.wiki import WikiStore
from modules.knowledge.worker import KnowledgeJobWorker
from test_concept_outline_tree import _outlined_pdf

FIXTURES = Path(__file__).resolve().parents[2] / "testdata" / "fixtures"
DEEP_LEARNING = FIXTURES / "深度学习-批量规范化.pdf"
NO_OUTLINE = FIXTURES / "os-cpu-scheduling.pdf"
# 大切片：d2l 第 4 章整章。上面两份只有十来页，任何上限都碰不到。
BIG = FIXTURES / "深度学习-多层感知机.pdf"
DEEP_LEARNING_PAGES = 10
NO_OUTLINE_PAGES = 13

needs_deep_learning = pytest.mark.skipif(
    not DEEP_LEARNING.exists(), reason=f"缺少切片教材 {DEEP_LEARNING.name}（scripts/e2e_fixture.py 生成）")
needs_no_outline = pytest.mark.skipif(
    not NO_OUTLINE.exists(), reason=f"缺少切片教材 {NO_OUTLINE.name}（scripts/e2e_fixture.py 生成）")
needs_big = pytest.mark.skipif(
    not BIG.exists(), reason=f"缺少大切片教材 {BIG.name}（scripts/e2e_fixture.py 生成）")


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


def _index_only(tmp_path, pdf: Path) -> Built:
    """只索引不写页。切段是纯函数，判「漏没漏」不需要真生成页面。"""
    course, service, worker, wiki_store, responder = _env(tmp_path)
    try:
        material = service.upload_material(course_id=course.id, filename=pdf.name,
                                           mime_type="application/pdf", content=pdf.read_bytes())
        job = _wait(service, worker, service.enqueue_index(material_id=material.id).id)
        assert job.status == "completed", job.error_message
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


def _outline(built: Built) -> list[dict]:
    """知识页真正用的目录：服务层从教材现算，不读 concepts 表（同名节在那张表里会被并掉）。"""
    material = built.service._repository.get_material(built.material_id)
    return built.service._wiki_outline(material)[0]


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


def _ids_named(documents: dict[str, str], name: str) -> list[str]:
    """按页名找页。页 id 按教材内的位置派生，同一个名字在两处讲就是两页，各自有 id。"""
    return [concept_id for concept_id, raw in documents.items()
            if _frontmatter(raw).get("concept_name") == name]


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
    pages = _ids_named(documents, "批量规范化")

    assert pages, "这门课最主要的概念必须有页"
    covered = set().union(*(_covered_pages(documents, concept_id) for concept_id in pages))
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
        material_id=built.material_id, concepts=_outline(built),
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


# ---- 大切片：整整一章，概念数远超节点上限 ----

@needs_big
def test_a_whole_chapter_over_the_node_cap_still_reads_every_chunk(tmp_path):
    """d2l 第 4 章整章：66 页、99 条书签、88 个概念，会被节点上限砍到装得下。

    十来页的切片碰不到任何上限，「超出上限会丢内容」这条路在它们身上走不到。
    """
    built = _build(tmp_path, BIG)
    fields = dict(item.split("=", 1) for item in (built.job.error_message or "").split()[1:])

    assert int(fields["concepts"]) > int(fields["pages"]), \
        f"这份教材本来就该顶到上限，不然测不到东西：{fields}"

    _pages, covered_chunks = _read_material(built)
    chunks = _chunk_ids(built)
    assert covered_chunks == chunks, \
        f"只读到 {len(covered_chunks & chunks)}/{len(chunks)} 个分片"


@needs_big
@pytest.mark.parametrize("max_nodes", [4, 12, 50])
def test_the_chapter_loses_nothing_at_any_node_cap(tmp_path, max_nodes):
    """真实教材上重跑「不漏」：切段是纯函数，不用真写页也能判。"""
    from modules.knowledge.wiki import plan_sections

    built = _index_only(tmp_path, BIG)
    chunks = built.service._repository.list_material_chunks(material_id=built.material_id)
    sections, stats = plan_sections(
        material_id=built.material_id, concepts=_outline(built), chunks=chunks, max_nodes=max_nodes)

    read = {chunk["id"] for section in sections for chunk in section.chunks}
    assert len(sections) <= max_nodes
    assert read == {chunk["id"] for chunk in chunks}, f"漏读 {len(chunks) - len(read)} 个分片"


@needs_big
@pytest.mark.parametrize("max_nodes", [4, 50])
def test_a_library_indexed_before_the_level_columns_existed_loses_nothing(tmp_path, max_nodes):
    """老库的概念行没有 level/parent_id/ordinal，取不到目录就该退回按分片切段。

    作者拿真书踩到的就是这个形态：三列是后加的，加之前索引过的教材一列都没有。
    知识页现在从教材现算目录，只有教材文件丢了才退回这张表，那时就会看到这个形态。
    低上限那一档不能省：这条路上没有上级页接住被砍掉的段，段数顶到上限时
    要靠把段放大来装下，砍掉尾巴就是整段原文一个字都不进知识页。
    """
    from modules.knowledge.wiki import plan_sections

    built = _index_only(tmp_path, BIG)
    with built.service._repository._store.write() as connection:
        connection.execute(
            "UPDATE concepts SET level = NULL, parent_id = NULL, ordinal = NULL WHERE material_id = ?",
            (built.material_id,))
    concepts = built.service._repository.list_material_concept_tree(material_id=built.material_id)
    assert concepts and all(row["level"] is None for row in concepts), "这个用例要的就是三列全空"

    chunks = built.service._repository.list_material_chunks(material_id=built.material_id)
    sections, stats = plan_sections(material_id=built.material_id, concepts=concepts, chunks=chunks,
                                    max_nodes=max_nodes)

    read = {chunk["id"] for section in sections for chunk in section.chunks}
    assert len(sections) <= max_nodes
    assert read == {chunk["id"] for chunk in chunks}, f"漏读 {len(chunks) - len(read)} 个分片"


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


def test_the_course_index_merges_the_top_pages_of_every_material(tmp_path):
    """一门课的几份教材共用一张课程首页。只装过一份教材时，跨教材聚合这条路走不到——
    首页会看着很对，其实只有最后构建的那一份在里面。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        _index_and_build(service, worker, course_id=course.id, filename="调度.pdf",
                         mime_type="application/pdf",
                         content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO"])]))
        _index_and_build(service, worker, course_id=course.id, filename="内存.pdf",
                         mime_type="application/pdf",
                         content=_outlined_pdf([("第 2 章 内存", ["2.1 分页"])]))
    finally:
        worker.shutdown()

    index = store.read(course_id=course.id, concept_id="index")
    for name in ("调度", "FIFO", "内存", "分页"):
        assert name in index, f"首页目录里少了 {name}"
    # 目录列全了还不够：首页正文读的是顶层页，两份教材的顶层页都要进证据。
    assert sorted(_refs(index)) == ["顶层页 内存", "顶层页 调度"], _refs(index)


# ---- 孤儿页清理：三条判断分支都要真的把文件删掉 ----

def _pruned(job) -> int:
    fields = dict(item.split("=", 1) for item in (job.error_message or "").split()[1:])
    return int(fields.get("pruned", -1))


def _ids_of(store: WikiStore, course_id: str, material_id: str) -> set[str]:
    return {page.concept_id for page in store.list_pages(course_id=course_id)
            if page.material_id == material_id}


def test_deleting_a_material_takes_its_pages_away_at_the_next_build(tmp_path):
    """删掉一份教材后它的知识页还留在盘上，读起来像这门课还讲着那些内容。
    下一次构建要把它们清掉，同课别的教材的页一页不少。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        gone, _job = _index_and_build(service, worker, course_id=course.id, filename="要删的.md",
                                      mime_type="text/markdown", content="# 极限\n\n极限描述趋势。\n".encode())
        kept, _job = _index_and_build(service, worker, course_id=course.id, filename="留下的.md",
                                      mime_type="text/markdown", content="# 连续性\n\n连续建立在极限之上。\n".encode())
        doomed = _ids_of(store, course.id, gone.id)
        survivors = _ids_of(store, course.id, kept.id)
        assert doomed and survivors, "两份教材都得各自写出页，不然测不到删谁留谁"

        service._repository.delete_material(gone.id)
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=kept.id).id)
    finally:
        worker.shutdown()

    left = {page.concept_id for page in store.list_pages(course_id=course.id)}
    assert not doomed & left, f"被删教材的页还在：{sorted(doomed & left)}"
    assert survivors <= left, f"别的教材的页被误删：{sorted(survivors - left)}"
    assert "index" in left, "课程首页是课程级的，任何时候都不删"
    assert _pruned(rebuilt) == len(doomed), rebuilt.error_message


def test_a_new_edition_drops_the_pages_of_sections_that_no_longer_exist(tmp_path):
    """教材换版后目录变了，小节 id 跟着变。旧页不清掉，知识页里就并排摆着两版的小节。

    两版都有的那一节要留着同一个 id：id 按树内路径派生，那一节的路径没变。
    """
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(
            service, worker, course_id=course.id, filename="教材.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO", "1.2 SJF"])]))
        before = _ids_of(store, course.id, material.id)
        unchanged = {page.concept_id for page in store.list_pages(course_id=course.id)
                     if page.concept_name == "调度"}

        # 换版：正文不动，只把目录换掉，重建后小节页只剩「调度」还对得上。
        path = service._repository.material_storage_path(material.id)
        path.write_bytes(_outlined_pdf([("第 1 章 调度", ["1.1 轮转"])]))
        service.parse_structure(material_id=material.id)
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    after = _ids_of(store, course.id, material.id)
    names = {page.concept_name for page in store.list_pages(course_id=course.id)}
    assert unchanged, "章级那一页得先写出来，不然测不到「两版都有」"
    assert not {"FIFO", "SJF"} & names, f"上一版的小节页还在：{sorted({'FIFO', 'SJF'} & names)}"
    assert "轮转" in names, "新版的小节应当写出来"
    assert unchanged <= after, "两版都有的那一节不该被顺手删掉"
    assert _pruned(rebuilt) == len(before - after), rebuilt.error_message


def test_pages_written_before_ownership_was_recorded_are_judged_by_the_concept_table(tmp_path):
    """老版本写的页没记教材归属，只能照概念表判：概念还在就留，概念没了就清。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(service, worker, course_id=course.id, filename="讲义.md",
                                          mime_type="text/markdown", content="# 极限\n\n极限描述趋势。\n".encode())
        alive = sorted(service._repository.concept_ids(course_id=course.id))[0]
        for concept_id in (alive, "legacy_gone"):
            store.write(course_id=course.id, concept_id=concept_id, concept_name=concept_id,
                        body="老版本写下的正文", source_hash="legacy", source_refs=[],
                        updated_at="2026-07-01T00:00:00+00:00")
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    left = {page.concept_id for page in store.list_pages(course_id=course.id)}
    assert "legacy_gone" not in left, "概念表里没有的老页应当被清掉"
    assert alive in left, "概念还在的老页不能删"
    assert _pruned(rebuilt) == 1, rebuilt.error_message


# ---- 同名小节：撞名的那几节各自成页，不能被并进别人的页码区间 ----

def _owned(store: WikiStore, course_id: str, material_id: str) -> list:
    return [page for page in store.list_pages(course_id=course_id) if page.material_id == material_id]


def test_two_materials_with_the_same_section_name_each_keep_their_own_pages(tmp_path):
    """一门课两份教材各有一节叫「反向传播」、一节叫「卷积神经网络」。

    concepts 表按「课程 + 名字」给 id 并在同名时并成一行，第二份那两节整节查不到自己名下，
    它们的原文会被并进邻节、贴着别人的标题，语义标签就错位了。判据落在两处：每份教材的
    目录节数要等于它名下的落盘页数，同名页的出处只能落在自己那份教材的页码区间里。
    """
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        first, _job = _index_and_build(
            service, worker, course_id=course.id, filename="甲.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 基础", ["1.1 反向传播", "1.2 梯度下降"]),
                                   ("第 2 章 网络", ["2.1 卷积神经网络", "2.2 循环神经网络"])]))
        second, _job = _index_and_build(
            service, worker, course_id=course.id, filename="乙.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 视觉", ["1.1 卷积神经网络", "1.2 池化"]),
                                   ("第 2 章 训练", ["2.1 反向传播", "2.2 学习率"])]))
    finally:
        worker.shutdown()

    named: dict[str, dict[str, str]] = {}
    for material in (first, second):
        outline = service._wiki_outline(service._repository.get_material(material.id))[0]
        pages = _owned(store, course.id, material.id)
        assert len(pages) == len(outline) == 6, \
            f"{material.filename}：目录 {len(outline)} 节，落盘 {len(pages)} 页"
        named[material.filename] = {page.concept_name: page.concept_id for page in pages}
    for name in ("反向传播", "卷积神经网络"):
        ids = {filename: pages[name] for filename, pages in named.items()}
        assert len(set(ids.values())) == 2, f"{name} 在两份教材里共用了同一页：{ids}"

    # 「乙」的反向传播是第 2 章的首个小节：区间从章首页 4 起（章导语要有人读）、往后多带一页
    # （跨页标题）。出处只能落在这个区间里，不能跑到别的教材、也不能跑到本教材的另一章去。
    raw = store.read(course_id=course.id, concept_id=named["乙.pdf"]["反向传播"])
    assert {ref.split(" p.")[0] for ref in _refs(raw)} == {"乙.pdf"}, _refs(raw)
    assert _pages_in("\n".join(_refs(raw))) <= {4, 5, 6}, _refs(raw)


def test_repeated_section_names_in_one_material_each_get_a_page(tmp_path):
    """d2l 每章都有「小结」：同一份教材内的重名节也要各自成页。

    id 只认「名字 + 教材内同名位次」，同一章下并排两节同名的最吃紧——祖先路径分不开它们，
    只有位次能。位次由书签顺序定，所以重算目录得到的还是同一批 id，增量刷新认得出同一节。
    """
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(
            service, worker, course_id=course.id, filename="教材.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 线性回归", ["1.1 小结", "1.2 从零实现", "1.3 小结"]),
                                   ("第 2 章 多层感知机", ["2.1 激活函数", "2.2 小结"])]))
    finally:
        worker.shutdown()

    pages = _owned(store, course.id, material.id)
    summaries = [page for page in pages if page.concept_name == "小结"]
    assert len(summaries) == 3, [page.concept_name for page in pages]
    assert len({page.concept_id for page in summaries}) == 3, "重名的几节共用了同一页"
    siblings = [page for page in summaries if page.parent_id == summaries[0].parent_id]
    assert len(siblings) == 2, "同一章下并排的那两节小结应当各自成页"

    again = {row["id"] for row in service._wiki_outline(service._repository.get_material(material.id))[0]}
    assert {page.concept_id for page in pages} == again, "重算目录得到的 id 应当和落盘的一模一样"


def test_renaming_a_chapter_leaves_its_leaf_pages_alone(tmp_path):
    """改一章的名字只该作废那一页。id 里带祖先路径的话整棵子树会跟着换 id，
    连用户在叶子页手写的内容一起没了——手写区没有第二份副本。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(
            service, worker, course_id=course.id, filename="教材.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO", "1.2 SJF"])]))
        leaves = {page.concept_name: page.concept_id for page in _owned(store, course.id, material.id)
                  if page.concept_name in {"FIFO", "SJF"}}
        root = tmp_path / "data" / "wiki" / course.id
        page_file = next(path for path in root.rglob("*FIFO.md"))
        page_file.write_text(page_file.read_text(encoding="utf-8") + "我自己补的一句，别弄丢。\n",
                             encoding="utf-8")

        service._repository.material_storage_path(material.id).write_bytes(
            _outlined_pdf([("第 1 章 处理机调度", ["1.1 FIFO", "1.2 SJF"])]))
        _wait(service, worker, service.enqueue_index(material_id=material.id).id)
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    assert rebuilt.status == "completed", rebuilt.error_message
    pages = _owned(store, course.id, material.id)
    after = {page.concept_name: page.concept_id for page in pages}
    assert after.get("FIFO") == leaves["FIFO"] and after.get("SJF") == leaves["SJF"], \
        f"改章名把叶子页也作废了：{leaves} → {after}"
    # 作废重建会把旧 id 那份留成孤儿（手写区不删），页数一多就说明叶子被连坐了。
    assert len(pages) == 3, [page.concept_name for page in pages]
    assert "我自己补的一句" in store.read(course_id=course.id, concept_id=leaves["FIFO"])


def test_a_page_the_user_wrote_in_is_not_pruned_when_its_section_disappears(tmp_path):
    """小节从目录里消失了，但用户在那一页写过东西。手写区没有第二份副本，
    这样的页留在原位当孤儿，不删。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(
            service, worker, course_id=course.id, filename="教材.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO", "1.2 SJF"])]))
        doomed = next(page.concept_id for page in _owned(store, course.id, material.id)
                      if page.concept_name == "SJF")
        root = tmp_path / "data" / "wiki" / course.id
        page_file = next(path for path in root.rglob("*SJF.md"))
        page_file.write_text(page_file.read_text(encoding="utf-8") + "这一节我自己整理过。\n",
                             encoding="utf-8")

        service._repository.material_storage_path(material.id).write_bytes(
            _outlined_pdf([("第 1 章 调度", ["1.1 FIFO"])]))
        _wait(service, worker, service.enqueue_index(material_id=material.id).id)
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    assert rebuilt.status == "completed", rebuilt.error_message
    assert "这一节我自己整理过" in store.read(course_id=course.id, concept_id=doomed)
    assert _pruned(rebuilt) == 0, rebuilt.error_message


def test_two_materials_with_the_same_filename_get_a_directory_each(tmp_path):
    """同课两份教材重名：目录名要错开。挤进同一个目录，两棵树的小节会混在同一章下面，
    同号同名的还会互相盖掉——盖掉是静默丢页，构建汇总还报着写成功。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        first, _job = _index_and_build(
            service, worker, course_id=course.id, filename="讲义.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 基础理论", ["1.1 反向传播"])]))
        second, _job = _index_and_build(
            service, worker, course_id=course.id, filename="讲义.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 基础理论", ["1.1 学习率"])]))
    finally:
        worker.shutdown()

    for material in (first, second):
        assert len(_owned(store, course.id, material.id)) == 2, \
            f"{material.id} 只剩 {len(_owned(store, course.id, material.id))} 页"
    root = tmp_path / "data" / "wiki" / course.id
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*.md") if path.is_file()) == [
        "index.md",
        "讲义-2/1-基础理论.md",
        "讲义-2/1-基础理论/1.1-学习率.md",
        "讲义/1-基础理论.md",
        "讲义/1-基础理论/1.1-反向传播.md",
    ]


def test_adding_a_chapter_renumbers_the_whole_library_without_leftovers(tmp_path):
    """章数从 9 到 10，编号宽度从一位变两位。前几章证据没变、不重写，但页要跟着搬——
    不搬就是一位和两位的编号在同一层混排，还留着一份旧的。"""
    chapters = [(f"第 {n} 章 主题{n}0", [f"{n}.1 小节{n}0"]) for n in range(1, 10)]
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(service, worker, course_id=course.id, filename="教材.pdf",
                                          mime_type="application/pdf", content=_outlined_pdf(chapters))
        service._repository.material_storage_path(material.id).write_bytes(
            _outlined_pdf(chapters + [("第 10 章 主题一百", ["10.1 小节一百"])]))
        _wait(service, worker, service.enqueue_index(material_id=material.id).id)
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    assert rebuilt.status == "completed", rebuilt.error_message
    root = tmp_path / "data" / "wiki" / course.id
    files = sorted(str(path.relative_to(root)) for path in root.rglob("*.md") if path.is_file())
    assert len(files) == 21, files  # 10 章 + 10 节 + 首页
    chapters_on_disk = sorted(name for name in files if name.count("/") == 1)
    assert chapters_on_disk == [f"教材/{n:02d}-主题{'一百' if n == 10 else f'{n}0'}.md" for n in range(1, 11)], \
        chapters_on_disk


def test_a_missing_material_file_falls_back_to_the_concept_table_and_says_so(tmp_path):
    """教材文件丢了取不到书签。退回概念表，并把数据源报进汇总——降质不该是静默的。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, job = _index_and_build(
            service, worker, course_id=course.id, filename="教材.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO"])]))
        assert "outline=material" in (job.error_message or ""), job.error_message
        service._repository.material_storage_path(material.id).unlink()
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    assert rebuilt.status == "completed", rebuilt.error_message
    assert "outline=concepts" in (rebuilt.error_message or ""), rebuilt.error_message


# ---- 可读落点：磁盘上的库能直接当笔记库打开 ----

def test_the_library_on_disk_is_laid_out_like_a_note_vault(tmp_path):
    """教材各占一个目录，页名带树内编号，中间页与它的子目录同名，index.md 仍在课程根。"""
    course, service, worker, _store, _responder = _env(tmp_path)
    try:
        _index_and_build(service, worker, course_id=course.id, filename="讲义.pdf",
                         mime_type="application/pdf",
                         content=_outlined_pdf([("第 1 章 机器学习基础", ["1.1 什么是机器学习", "1.2 监督学习"]),
                                                ("第 2 章 神经网络", ["2.1 感知机"])]))
    finally:
        worker.shutdown()

    root = tmp_path / "data" / "wiki" / course.id
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*.md") if path.is_file()) == [
        "index.md",
        "讲义/1-机器学习基础.md",
        "讲义/1-机器学习基础/1.1-什么是机器学习.md",
        "讲义/1-机器学习基础/1.2-监督学习.md",
        "讲义/2-神经网络.md",
        "讲义/2-神经网络/2.1-感知机.md",
    ]


def test_renumbering_a_page_moves_it_instead_of_leaving_a_second_copy(tmp_path):
    """证据没变的页不重写，编号却会随目录改动而变。不搬走，同一目录里就并排摆着两个同号的页。"""
    store = WikiStore(tmp_path)
    store.write(course_id="c1", concept_id="s1", concept_name="小结", body="正文",
                source_hash="h1", source_refs=[], updated_at="2026-08-06T00:00:00Z",
                location=("讲义", "1.2-小结"))
    store.relocate(course_id="c1", concept_id="s1", location=("讲义", "1.3-小结"))

    root = tmp_path / "wiki" / "c1"
    assert [str(path.relative_to(root)) for path in root.rglob("*.md")] == ["讲义/1.3-小结.md"]
    assert "正文" in store.read(course_id="c1", concept_id="s1")


def test_a_renamed_page_keeps_the_handwritten_area_and_leaves_no_copy_behind(tmp_path):
    """换版让小节换了编号，页要搬到新落点，手写区跟着走，旧文件不留在库里。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        material, _job = _index_and_build(
            service, worker, course_id=course.id, filename="教材.pdf", mime_type="application/pdf",
            content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO", "1.2 SJF"])]))
        page = next(page for page in store.list_pages(course_id=course.id) if page.concept_name == "SJF")
        root = tmp_path / "data" / "wiki" / course.id
        before = next(path for path in root.rglob("*SJF.md"))
        before.write_text(before.read_text(encoding="utf-8") + "我自己补的一句，别弄丢。\n", encoding="utf-8")

        # 换版：中间插一节，SJF 从 1.2 变成 1.3。
        service._repository.material_storage_path(material.id).write_bytes(
            _outlined_pdf([("第 1 章 调度", ["1.1 FIFO", "1.2 轮转", "1.3 SJF"])]))
        _wait(service, worker, service.enqueue_index(material_id=material.id).id)
        rebuilt = _wait(service, worker, service.enqueue_wiki_build(material_id=material.id).id)
    finally:
        worker.shutdown()

    assert rebuilt.status == "completed", rebuilt.error_message
    after = [path for path in root.rglob("*SJF.md")]
    assert [path.name for path in after] == ["1.3-SJF.md"], [str(path) for path in after]
    raw = store.read(course_id=course.id, concept_id=page.concept_id)
    assert "我自己补的一句" in raw, raw


# ---- 落点安全：认页只认 frontmatter，写页不越出课程目录 ----

def _write_page(store: WikiStore, concept_id: str, name: str, location: tuple[str, ...], body: str = "正文"):
    return store.write(course_id="c1", concept_id=concept_id, concept_name=name, body=body,
                       source_hash="h", source_refs=[], updated_at="2026-08-06T00:00:00Z",
                       location=location)


def test_a_rewrite_under_a_symlinked_ancestor_keeps_the_page(tmp_path):
    """数据目录挂在软链下面（macOS 的 /var 就是）：扫出来的路径没解析、写页的解析过，
    同一份文件两个写法，重写时「收掉旧文件」那一步会把刚写好的页删掉。"""
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "target", target_is_directory=True)
    _write_page(WikiStore(tmp_path / "link" / "data"), "s1", "甲", ("讲义", "1-甲"), body="第一遍")
    # 换个实例重写：落点只能靠扫目录拿到，和写页那侧算出来的必须是同一个写法。
    store = WikiStore(tmp_path / "link" / "data")
    _write_page(store, "s1", "甲", ("讲义", "1-甲"), body="第二遍")

    assert (tmp_path / "target" / "data" / "wiki" / "c1" / "讲义" / "1-甲.md").is_file()
    assert "第二遍" in store.read(course_id="c1", concept_id="s1")


def test_a_page_whose_landing_spot_is_taken_gets_its_own_file(tmp_path):
    """两页要落到同一个位置时各自成文件。同课重名的教材已经在目录层错开了，这是最后一道
    兜底——真撞上时宁可多一个后缀，也不能一份盖掉另一份。"""
    store = WikiStore(tmp_path)
    _write_page(store, "a", "小结", ("讲义", "1-小结"), body="第一页")
    _write_page(store, "b", "小结", ("讲义", "1-小结"), body="第二页")

    assert "第一页" in store.read(course_id="c1", concept_id="a")
    assert "第二页" in store.read(course_id="c1", concept_id="b")


def test_pages_are_found_through_a_symlinked_ancestor(tmp_path):
    """数据目录挂在软链下面时，扫出来的路径和课程目录必须解析到同一套写法上，
    否则每个文件都会被判成「跑出课程目录」，一页都列不出来。"""
    (tmp_path / "target").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "target", target_is_directory=True)
    store = WikiStore(tmp_path / "link" / "data")
    _write_page(store, "s1", "甲", ("讲义", "1-甲"))

    assert [page.concept_id for page in WikiStore(tmp_path / "link" / "data").list_pages(course_id="c1")] == ["s1"]


def test_a_file_the_user_dropped_in_is_neither_listed_nor_pruned(tmp_path):
    """库是给人打开的，用户会往里放自己的 markdown。没有 concept_id 的文件不是知识页，
    列不出来，清理孤儿页时也不能顺手删掉。"""
    store = WikiStore(tmp_path)
    _write_page(store, "s1", "甲", ("讲义", "1-甲"))
    mine = tmp_path / "wiki" / "c1" / "讲义" / "我的笔记.md"
    mine.write_text("# 我的笔记\n\n考前突击用。\n", encoding="utf-8")

    assert [page.concept_id for page in store.list_pages(course_id="c1")] == ["s1"]
    assert store.prune(course_id="c1", valid_concept_ids=set()) == ["s1"]
    assert mine.is_file(), "用户自己的文件被当成孤儿页删了"


@pytest.mark.parametrize("line", ["level: 一", "order: ²", "order: --5", "level: 1.5"])
def test_one_page_with_a_broken_frontmatter_line_does_not_take_the_course_down(tmp_path, line):
    """手改坏一行不该让整门课列不出、读不了、构建不下去。

    「能不能当数用」交给 int 判：² 的 isdigit 是真、int 不收，自己写判据必漏。
    """
    store = WikiStore(tmp_path)
    _write_page(store, "s1", "甲", ("讲义", "1-甲"))
    (tmp_path / "wiki" / "c1" / "讲义" / "坏页.md").write_text(
        f"---\nconcept_id: s9\n{line}\n---\n\n正文\n", encoding="utf-8")

    assert {page.concept_id for page in store.list_pages(course_id="c1")} == {"s1", "s9"}
    assert "正文" in store.read(course_id="c1", concept_id="s1")
    assert _write_page(store, "s2", "乙", ("讲义", "2-乙")).concept_id == "s2"


def test_a_page_saved_in_another_encoding_does_not_stop_the_cleanup(tmp_path):
    """按 GBK 存过的页解码不出来。认页那侧用 errors=replace 读得下去，所以它会一路走到
    清理孤儿页那一步——那里再抛解码错，整次构建就停了。"""
    store = WikiStore(tmp_path)
    _write_page(store, "s1", "甲", ("讲义", "1-甲"))
    (tmp_path / "wiki" / "c1" / "讲义" / "乙.md").write_bytes(
        "---\nconcept_id: s8\nconcept_name: 乙\n---\n\n".encode() + "中文正文".encode("gbk") + b"\n")

    assert sorted(store.prune(course_id="c1", valid_concept_ids={"s1"})) == ["s8"]


def test_a_symlink_pointing_out_of_the_course_directory_is_not_a_page(tmp_path):
    """软链能把读页带出课程目录。写页那侧一直拦着，读页这侧也要守同一条线。"""
    store = WikiStore(tmp_path)
    _write_page(store, "s1", "甲", ("讲义", "1-甲"))
    outside = tmp_path / "outside.md"
    outside.write_text("---\nconcept_id: leaked\n---\n\n课程外的内容\n", encoding="utf-8")
    (tmp_path / "wiki" / "c1" / "链接.md").symlink_to(outside)

    assert [page.concept_id for page in store.list_pages(course_id="c1")] == ["s1"]
    with pytest.raises(LookupError):
        store.read(course_id="c1", concept_id="leaked")


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


def test_the_node_cap_makes_pages_bigger_it_never_drops_content():
    """没有目录时上限之外的段没有上级页兜住，所以段要放大到装得下，不能砍掉尾巴。

    真实故障：一本 813 页的书切出 169 段、上限 50，119 段的原文一个字都没进知识页。
    """
    sections, stats = _plan([], _chunks(list(range(1, 21)), size=5000), max_nodes=4)

    assert len(sections) <= 4
    assert stats["candidates"] > 4, "这个用例本来就该顶到上限，不然测不到东西"
    assert stats["capped"] == stats["candidates"] - len(sections), "合并了多少要报出来"
    assert _leaf_pages(sections) == set(range(1, 21))


@pytest.mark.parametrize("max_nodes", [2, 4, 7, 50])
@pytest.mark.parametrize("with_outline", [True, False])
def test_every_chunk_is_read_by_some_section(max_nodes, with_outline):
    """不漏是这次改造的全部意义：任何配置下，每个分片都必须被某个 section 读到。

    判据落在分片上而不是页码上——页码粒度太粗，几段各查几页就能凑满整本书，
    旧实现在页码判据下是绿的，实际每份教材只读到了一半分片。
    """
    chunks = _chunks(list(range(1, 21)), size=3000)
    rows = [("章", 1, 0, None)] + [(f"节{index}", index + 1, 1, "章") for index in range(1, 15)]
    sections, stats = _plan(_concepts(rows) if with_outline else [], chunks, max_nodes=max_nodes)

    read = {chunk["id"] for section in sections for chunk in section.chunks}
    assert read == {chunk["id"] for chunk in chunks}, f"漏读 {len({c['id'] for c in chunks}) - len(read)} 个分片"


def test_a_backwards_bookmark_does_not_leave_a_section_reading_nothing():
    """目录里页码倒退时（扫描件重排、附录插在中间），区间会变成起点大于终点，
    那一节一个分片都取不到。夹到起始页，它至少还读得到自己那一页。"""
    concepts = [
        {"id": "a", "name": "第一章", "page": 1, "level": 0, "parent_id": None, "ordinal": 0},
        {"id": "b", "name": "第二章", "page": 5, "level": 0, "parent_id": None, "ordinal": 1},
        {"id": "c", "name": "附录", "page": 2, "level": 0, "parent_id": None, "ordinal": 2},
    ]
    chunks = [{"id": f"ch{page}", "page": page, "ordinal": page, "content": f"p{page}"} for page in range(1, 9)]
    from modules.knowledge.wiki import plan_sections

    sections, _ = plan_sections(material_id="m1", concepts=concepts, chunks=chunks)

    assert all(section.last_page >= section.first_page for section in sections), \
        [(s.name, s.first_page, s.last_page) for s in sections]
    assert all(section.chunks for section in sections if not section.children), \
        [s.name for s in sections if not s.children and not s.chunks]


def test_chunks_without_a_page_number_still_get_read():
    """按页码分段会漏掉没有页号的分片。真实教材里就有——d2l 2033 个分片里 51 个没有页号，
    页码覆盖 1~813 看着是满的，那 51 段却谁都没读。"""
    from modules.knowledge.wiki import plan_sections

    chunks = _chunks(list(range(1, 11)), size=800)
    for index in (3, 7):  # 提取不出页号的那几段
        chunks[index] = {**chunks[index], "page": None}
    rows = [("章", 1, 0, None)] + [(f"节{index}", index + 1, 1, "章") for index in range(1, 5)]
    sections, stats = plan_sections(material_id="m1", concepts=_concepts(rows), chunks=chunks)

    read = {chunk["id"] for section in sections for chunk in section.chunks}
    assert read == {chunk["id"] for chunk in chunks}, f"漏读 {len(chunks) - len(read)} 个"
