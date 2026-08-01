"""教材目录的层级要落到概念表里：谁挂在哪一章下面、每个节点覆盖到第几页。

层级来自 PDF 自带的书签，是作者写的结构。没有书签的教材（讲义、扫描件）拿不到层级，
那是正常情况：概念照常抽出，parent_id 与 level 留空，界面平铺。
"""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from core.settings import Settings
from core.store import SQLiteStore
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.concepts import concept_id_for, from_outline
from modules.knowledge.extract import pdf_outline
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker

FIXTURES = Path(__file__).resolve().parents[2] / "testdata" / "fixtures"
DEEP_LEARNING = FIXTURES / "深度学习-批量规范化.pdf"
NO_OUTLINE = FIXTURES / "os-cpu-scheduling.pdf"


# ---- 纯函数：from_outline 的层级还原 ----

def test_outline_carries_parent_and_level():
    rows = [(0, "第 5 章 微调", 10), (1, "5.1 LoRA", 11), (2, "5.1.1 秩分解", 12), (1, "5.2 前缀微调", 20)]
    by_name = {item["name"]: item for item in from_outline(rows)}

    assert by_name["微调"]["parent"] is None and by_name["微调"]["level"] == 0
    assert by_name["LoRA"]["parent"] == "微调" and by_name["LoRA"]["level"] == 1
    assert by_name["秩分解"]["parent"] == "LoRA" and by_name["秩分解"]["level"] == 2
    assert by_name["前缀微调"]["parent"] == "微调" and by_name["前缀微调"]["level"] == 1


def test_siblings_on_one_page_keep_document_order():
    """同一页里的几个小节靠文档顺序区分，不能退回按名字排。"""
    rows = [(0, "批量规范化层", 3), (1, "卷积层", 3), (1, "全连接层", 3), (1, "预测过程", 3)]
    children = [item["name"] for item in from_outline(rows) if item["parent"] == "批量规范化层"]

    assert children == ["卷积层", "全连接层", "预测过程"]


def test_a_dropped_ancestor_does_not_orphan_its_children():
    """前言这类节点被过滤掉，挂在它下面的概念改挂最近的存活祖先，而不是凭空多出一层。"""
    rows = [(0, "深度学习", 1), (1, "前言", 2), (2, "反向传播", 3)]
    by_name = {item["name"]: item for item in from_outline(rows)}

    assert "前言" not in by_name
    assert by_name["反向传播"]["parent"] == "深度学习" and by_name["反向传播"]["level"] == 1


def test_a_repeated_title_is_placed_once_and_later_copies_do_not_reparent_children():
    """同名派生同一个 concept_id，树上只能挂一处：留最浅、并列时留最先出现的那个位置。"""
    rows = [(1, "模型", 39), (2, "参数量", 40), (0, "模型", 12), (1, "数据", 38)]
    items = {item["name"]: item for item in from_outline(rows)}

    assert items["模型"]["page"] == 12 and items["模型"]["parent"] is None
    # 第一处「模型」没有建节点，它的子节点改挂最近的存活祖先（这里没有，成为根）。
    assert items["参数量"]["parent"] is None
    assert items["数据"]["parent"] == "模型"


# ---- 落库：父子关系、大小写合并、幂等 ----

def _repository(tmp_path, *, materials: tuple[str, ...] = ("m1",)):
    store = SQLiteStore(tmp_path / "cp.db")
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id,name,color,created_at,updated_at) VALUES ('course_x','测试课','#B56E3D','now','now')")
        for material_id in materials:
            conn.execute(
                "INSERT INTO materials(id,course_id,filename,storage_path,mime_type,byte_size,index_status,created_at,updated_at)"
                " VALUES (?,'course_x','book.pdf','/tmp/book.pdf','application/pdf',1,'indexed','now','now')", (material_id,))
    repository = KnowledgeRepository.__new__(KnowledgeRepository)
    repository._store = store
    return repository, store


def test_parent_and_level_reach_the_database(tmp_path):
    repository, store = _repository(tmp_path)

    repository.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "微调", "page": 10, "mention_count": 10, "level": 0, "parent": None},
                    {"name": "LoRA", "page": 11, "mention_count": 9, "level": 1, "parent": "微调"}],
    )

    tree = {row["name"]: row for row in repository.list_concept_tree(course_id="course_x")}
    assert tree["微调"]["parent_id"] is None and tree["微调"]["level"] == 0
    assert tree["LoRA"]["parent_id"] == concept_id_for("course_x", "微调") and tree["LoRA"]["level"] == 1
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_children_of_a_merged_case_variant_point_at_the_survivor(tmp_path):
    """父节点有两个大小写变体时只留一条，子节点不能指向被合并掉的那个 id。"""
    repository, store = _repository(tmp_path)

    repository.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "lora", "page": 9, "mention_count": 10, "level": 0, "parent": None},
                    {"name": "LoRA", "page": 3, "mention_count": 12, "level": 0, "parent": None},
                    {"name": "秩分解", "page": 4, "mention_count": 9, "level": 1, "parent": "lora"}],
    )

    tree = {row["name"]: row for row in repository.list_concept_tree(course_id="course_x")}
    assert set(tree) == {"LoRA", "秩分解"}
    assert tree["秩分解"]["parent_id"] == tree["LoRA"]["id"]
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_parent_that_did_not_survive_leaves_the_child_at_the_root(tmp_path):
    """父节点不在这一批候选里就不连边，宁可平铺也不能留一个悬空外键。"""
    repository, store = _repository(tmp_path)

    repository.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "秩分解", "page": 4, "mention_count": 9, "level": 1, "parent": "不存在的章"}],
    )

    assert repository.list_concept_tree(course_id="course_x")[0]["parent_id"] is None
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reindexing_reproduces_the_same_tree(tmp_path):
    repository, store = _repository(tmp_path)
    candidates = [{"name": "微调", "page": 10, "mention_count": 10, "level": 0, "parent": None},
                  {"name": "LoRA", "page": 11, "mention_count": 9, "level": 1, "parent": "微调"},
                  {"name": "秩分解", "page": 12, "mention_count": 8, "level": 2, "parent": "LoRA"}]

    repository.replace_material_concepts(course_id="course_x", material_id="m1", candidates=candidates)
    first = repository.list_concept_tree(course_id="course_x")
    repository.replace_material_concepts(course_id="course_x", material_id="m1", candidates=candidates)

    assert repository.list_concept_tree(course_id="course_x") == first
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_dropping_the_parent_concept_does_not_break_the_child_row(tmp_path):
    """重新索引时父节点没再抽到：子节点回到根，而不是让整个作业撞外键。"""
    repository, store = _repository(tmp_path)
    repository.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "微调", "page": 10, "mention_count": 10, "level": 0, "parent": None},
                    {"name": "LoRA", "page": 11, "mention_count": 9, "level": 1, "parent": "微调"}],
    )

    repository.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "LoRA", "page": 11, "mention_count": 9, "level": 0, "parent": None}],
    )

    rows = repository.list_concept_tree(course_id="course_x")
    assert [(row["name"], row["parent_id"]) for row in rows] == [("LoRA", None)]
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_material_takes_the_whole_tree_with_it(tmp_path):
    """删教材要能一次删掉整棵树，父子外键不该把删除拦下来。"""
    repository, store = _repository(tmp_path)
    repository.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "微调", "page": 10, "mention_count": 10, "level": 0, "parent": None},
                    {"name": "LoRA", "page": 11, "mention_count": 9, "level": 1, "parent": "微调"}],
    )

    assert repository.delete_material("m1") is not None

    assert repository.list_concept_tree(course_id="course_x") == []
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---- 端到端：真的索引一份 PDF ----

def _pdf_with_pages(page_texts: list[str]) -> bytes:
    """Build a minimal PDF with one text content stream per page."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(page_texts)))
    font_ref = 3 + len(page_texts) * 2
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_texts)} >>".encode())
    for index, text in enumerate(page_texts):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + index * 2} 0 R "
            f"/Resources << /Font << /F1 {font_ref} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >> stream\n%s\nendstream" % (len(content), content))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj ".encode() + body + b" endobj\n")
    xref_at = buffer.tell()
    buffer.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode())
    return buffer.getvalue()


def _outlined_pdf(chapters: list[tuple[str, list[str]]]) -> bytes:
    """一页一节的 PDF（有文字层），并按 chapters 写进目录书签。"""
    from pypdf import PdfReader, PdfWriter

    titles = [title for chapter, sections in chapters for title in (chapter, *sections)]
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(_pdf_with_pages(
        [f"page {number} body text" for number, _title in enumerate(titles, start=1)]))))
    index = 0
    for title, sections in chapters:
        parent = writer.add_outline_item(title, index)
        index += 1
        for section in sections:
            writer.add_outline_item(section, index, parent=parent)
            index += 1
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _env(tmp_path):
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=200, chunk_overlap=20, top_k_results=6,
        material_max_bytes=20 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    course = CourseService(CourseRepository(store)).create_course(name="测试课")
    repository = KnowledgeRepository(store)
    service = KnowledgeService(repository=repository, settings=settings, wiki_is_enabled=lambda _id: False)
    worker = KnowledgeJobWorker(service, workers=1, queue_capacity=4)
    worker.start()
    return settings, store, course, repository, service, worker


def _index(service, worker, *, course_id: str, filename: str, content: bytes):
    material = service.upload_material(course_id=course_id, filename=filename,
                                       mime_type="application/pdf", content=content)
    job = service.enqueue_index(material_id=material.id)
    assert worker.submit(job.id)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = service.get_job(job_id=job.id)
        if current and current.status in {"completed", "failed"}:
            return material, current
        time.sleep(0.02)
    pytest.fail("索引任务没有进入终态")


def test_a_pdf_outline_becomes_a_tree_in_the_database(tmp_path):
    _settings, store, course, repository, service, worker = _env(tmp_path)
    try:
        _index(service, worker, course_id=course.id, filename="book.pdf",
               content=_outlined_pdf([("第 1 章 调度", ["1.1 FIFO", "1.2 SJF"]), ("第 2 章 内存", ["2.1 分页"])]))
    finally:
        worker.shutdown()

    tree = {row["name"]: row for row in repository.list_concept_tree(course_id=course.id)}
    assert set(tree) == {"调度", "FIFO", "SJF", "内存", "分页"}
    assert tree["FIFO"]["parent_id"] == tree["调度"]["id"] and tree["FIFO"]["level"] == 1
    assert tree["分页"]["parent_id"] == tree["内存"]["id"]
    assert tree["调度"]["parent_id"] is None and tree["调度"]["level"] == 0
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_a_pdf_without_bookmarks_stays_flat(tmp_path):
    """没有书签就走刮正文那条路：概念照常抽出，层级两列全空。"""
    _settings, _store, course, repository, service, worker = _env(tmp_path)
    body = "# Round Robin\n\n轮转调度把时间片分给每个任务。\n\n# 上下文切换\n\n切换有开销。\n"
    try:
        material = service.upload_material(course_id=course.id, filename="notes.md",
                                           mime_type="text/markdown", content=body.encode())
        job = service.enqueue_index(material_id=material.id)
        assert worker.submit(job.id)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = service.get_job(job_id=job.id)
            if current and current.status in {"completed", "failed"}:
                break
            time.sleep(0.02)
    finally:
        worker.shutdown()

    rows = repository.list_concept_tree(course_id=course.id)
    assert rows, "没有书签也要抽出概念"
    assert all(row["parent_id"] is None and row["level"] is None for row in rows)


def test_the_http_endpoint_serves_the_tree(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "data" / "coursepilot.db",
        uploads_dir=tmp_path / "data" / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=200, chunk_overlap=20, top_k_results=6,
        material_max_bytes=20 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    with TestClient(create_app(settings=settings)) as client:
        course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
        upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                             files={"file": ("book.pdf", _outlined_pdf([("第 1 章 调度", ["1.1 FIFO"])]), "application/pdf")})
        assert upload.status_code == 201, upload.text
        job = client.post(f"/api/v2/materials/{upload.json()['id']}/index").json()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            current = client.get(f"/api/v2/jobs/{job['id']}").json()
            if current["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert current["status"] == "completed", current

        payload = client.get(f"/api/v2/courses/{course['id']}/concepts")
        assert payload.status_code == 200, payload.text
        nodes = {node["name"]: node for node in payload.json()["concepts"]}
        assert nodes["FIFO"]["parent_id"] == nodes["调度"]["id"]
        assert nodes["调度"]["level"] == 0 and nodes["FIFO"]["level"] == 1
        assert client.get("/api/v2/courses/course_missing/concepts").status_code == 404


# ---- 真实教材：整棵树逐条核对 ----

@pytest.mark.skipif(not DEEP_LEARNING.exists(), reason=f"缺少切片教材 {DEEP_LEARNING.name}（scripts/example_setup.py 生成）")
def test_the_real_textbook_tree_matches_its_pdf_outline_row_by_row(tmp_path):
    """主判据：13 条书签、三层，落库后每个节点的父节点、层级、页码都和目录一致。

    期望值直接从 PDF 的 outline 现算，不手抄。
    """
    _settings, store, course, repository, service, worker = _env(tmp_path)
    try:
        _material, job = _index(service, worker, course_id=course.id, filename=DEEP_LEARNING.name,
                                content=DEEP_LEARNING.read_bytes())
    finally:
        worker.shutdown()
    assert job.status == "completed", job.error_message

    outline = pdf_outline(DEEP_LEARNING)
    assert len(outline) == 13 and {level for level, _t, _p in outline} == {0, 1, 2}
    # 目录顺序遍历，用一个栈把「上一层最近的标题」当作父节点，得到期望的树。
    expected, stack = {}, []
    for level, title, page in outline:
        del stack[level:]
        expected[title] = (stack[-1] if stack else None, level, page)
        stack.append(title)

    rows = repository.list_concept_tree(course_id=course.id)
    by_id = {row["id"]: row for row in rows}
    actual = {row["name"]: (by_id[row["parent_id"]]["name"] if row["parent_id"] else None, row["level"], row["page"])
              for row in rows}

    assert actual == expected
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.skipif(not NO_OUTLINE.exists(), reason=f"缺少切片教材 {NO_OUTLINE.name}（scripts/example_setup.py 生成）")
def test_the_real_textbook_without_bookmarks_yields_flat_concepts(tmp_path):
    _settings, _store, course, repository, service, worker = _env(tmp_path)
    try:
        _material, job = _index(service, worker, course_id=course.id, filename=NO_OUTLINE.name,
                                content=NO_OUTLINE.read_bytes())
    finally:
        worker.shutdown()
    assert job.status == "completed", job.error_message

    assert pdf_outline(NO_OUTLINE) == []
    rows = repository.list_concept_tree(course_id=course.id)
    assert rows, "没有书签也要抽出概念"
    assert all(row["parent_id"] is None and row["level"] is None for row in rows)
