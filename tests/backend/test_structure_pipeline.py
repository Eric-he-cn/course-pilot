"""目录结构解析从 RAG 索引流水线里拆出来，可以单独重算。

检索索引管向量与 FTS，目录结构管概念与层级，两条共享已落库的正文。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from core.settings import Settings
from conftest import workspace
from test_concept_outline_tree import _outlined_pdf

FULL_OUTLINE = [("第 1 章 调度", ["1.1 FIFO", "1.2 SJF"]), ("第 2 章 内存", ["2.1 分页"])]
TRIMMED_OUTLINE = [("第 1 章 调度", ["1.1 FIFO"])]
# 标题与 FULL_OUTLINE 完全重合：同名概念归先抽到它的那份教材，这一份一个都不归它。
SHARED_OUTLINE = [("第 1 章 调度", ["1.1 FIFO"])]
MARKDOWN_NOTES = "\n\n".join(
    f"## {title}\n这一节讲 {title} 的做法与代价，篇幅足够被切成一块正文。"
    for title in ("进程调度", "虚拟内存", "文件系统")
)


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "data" / "coursepilot.db",
        uploads_dir=tmp_path / "data" / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=200, chunk_overlap=20, top_k_results=6,
        material_max_bytes=20 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _await_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.02)
    pytest.fail(f"任务 {job_id} 没有进入终态")


def _indexed_pdf(client: TestClient, *, chapters=FULL_OUTLINE, name="操作系统") -> tuple[str, str]:
    course = client.post("/api/v2/courses", json={"name": name}).json()
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("book.pdf", _outlined_pdf(chapters), "application/pdf")})
    assert upload.status_code == 201, upload.text
    material_id = upload.json()["id"]
    job = client.post(f"/api/v2/materials/{material_id}/index").json()
    assert _await_job(client, job["id"])["status"] == "completed"
    return course["id"], material_id


def _index_more(client: TestClient, course_id: str, *, chapters, filename="second.pdf") -> str:
    """往同一门课再加一份教材并索引完。"""
    upload = client.post(f"/api/v2/courses/{course_id}/materials",
                         files={"file": (filename, _outlined_pdf(chapters), "application/pdf")})
    assert upload.status_code == 201, upload.text
    job = client.post(f"/api/v2/materials/{upload.json()['id']}/index").json()
    assert _await_job(client, job["id"])["status"] == "completed"
    return upload.json()["id"]


def _status_of(client: TestClient, course_id: str, material_id: str) -> dict:
    payload = client.get(f"/api/v2/courses/{course_id}/structure").json()
    return next(row for row in payload["materials"] if row["material_id"] == material_id)


def _index_status(client: TestClient, course_id: str, material_id: str) -> str:
    rows = client.get(f"/api/v2/courses/{course_id}/materials").json()
    return next(row["status"] for row in rows if row["id"] == material_id)


def _chunk_rows(client: TestClient) -> list[tuple]:
    """整张 chunks 表的逐字节快照，向量也带上。"""
    with workspace(client).store.read() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT id, material_id, course_id, ordinal, page, content, embedding, source_kind"
            " FROM chunks ORDER BY id")]


def _fts_rows(client: TestClient) -> list[tuple]:
    with workspace(client).store.read() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT chunk_id, course_id, content FROM chunks_fts ORDER BY chunk_id")]


def _concept_rows(client: TestClient, course_id: str) -> dict[str, tuple]:
    with workspace(client).store.read() as conn:
        return {row["name"]: (row["id"], row["parent_id"], row["level"], row["ordinal"])
                for row in conn.execute("SELECT * FROM concepts WHERE course_id = ?", (course_id,))}


def _plant_embeddings(client: TestClient) -> None:
    """本机测试没有向量模型，先塞进去几串字节，才谈得上「向量没被动过」。"""
    with workspace(client).store.write() as conn:
        for row in conn.execute("SELECT id FROM chunks").fetchall():
            conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (f"vec-{row['id']}".encode(), row["id"]))


# ---- 拆分本身：结构可以单独重算，重算不碰检索侧 ----

def test_reparsing_the_structure_leaves_chunks_and_vectors_untouched(client):
    """判据是逐字节：结构那条路只写 concepts，chunks、向量、FTS 一行都不该变。"""
    course_id, material_id = _indexed_pdf(client)
    _plant_embeddings(client)
    chunks_before, fts_before = _chunk_rows(client), _fts_rows(client)
    # 模拟层级三列上线前索引过的老库：概念还在，但树没了。
    with workspace(client).store.write() as conn:
        conn.execute("DELETE FROM concepts WHERE course_id = ?", (course_id,))

    response = client.post(f"/api/v2/materials/{material_id}/structure")

    assert response.status_code == 200, response.text
    tree = _concept_rows(client, course_id)
    assert set(tree) == {"调度", "FIFO", "SJF", "内存", "分页"}
    assert tree["FIFO"][1] == tree["调度"][0] and tree["FIFO"][2] == 1
    assert tree["调度"][1] is None and tree["调度"][3] == 0
    assert _chunk_rows(client) == chunks_before
    assert _fts_rows(client) == fts_before


def test_a_failing_outline_pass_no_longer_takes_the_search_index_down_with_it(client):
    """结构解析出错时检索索引照样完成。拆开之前它们是同一个 try，一起失败。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    space = workspace(client)

    def boom(*_args, **_kwargs):
        raise RuntimeError("目录解析炸了")

    space.knowledge._concepts_for = boom
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("book.pdf", _outlined_pdf(FULL_OUTLINE), "application/pdf")})
    job = client.post(f"/api/v2/materials/{upload.json()['id']}/index").json()

    finished = _await_job(client, job["id"])

    assert finished["status"] == "completed", finished
    assert client.get(f"/api/v2/courses/{course['id']}/materials").json()[0]["status"] == "indexed"
    assert client.post(f"/api/v2/courses/{course['id']}/knowledge/search", json={"query": "body"}).json()
    assert _concept_rows(client, course["id"]) == {}


def test_uploading_still_produces_the_whole_tree_without_a_second_click(client):
    """默认体验不变：索引跑完概念、层级、文档顺序都在，用户没有多点一次。"""
    course_id, _material_id = _indexed_pdf(client)

    tree = _concept_rows(client, course_id)

    assert set(tree) == {"调度", "FIFO", "SJF", "内存", "分页"}
    assert [name for name, row in sorted(tree.items(), key=lambda item: item[1][3])] == \
        ["调度", "FIFO", "SJF", "内存", "分页"]
    assert all(row[2] is not None and row[3] is not None for row in tree.values())


# ---- 结构状态：从 concepts 表推导，不加列 ----

def test_a_material_indexed_before_the_hierarchy_existed_says_so_instead_of_going_flat(client):
    """老库里 level 全空。状态要能说出「有概念但没有层级」，界面才不用从长相反推。"""
    course_id, material_id = _indexed_pdf(client)
    with workspace(client).store.write() as conn:
        conn.execute("UPDATE concepts SET level = NULL, parent_id = NULL, ordinal = NULL WHERE course_id = ?",
                     (course_id,))

    stale = client.get(f"/api/v2/courses/{course_id}/structure")

    assert stale.status_code == 200, stale.text
    entry = next(item for item in stale.json()["materials"] if item["material_id"] == material_id)
    assert entry["concepts"] == 5 and entry["leveled"] == 0
    assert entry["has_structure"] is True and entry["has_levels"] is False

    client.post(f"/api/v2/materials/{material_id}/structure")

    entry = next(item for item in client.get(f"/api/v2/courses/{course_id}/structure").json()["materials"]
                 if item["material_id"] == material_id)
    assert entry["leveled"] == 5 and entry["has_levels"] is True


def test_structure_status_reports_a_material_that_has_no_concepts_at_all(client):
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("notes.md", "散文一段，没有任何标题结构。", "text/markdown")})

    entry = client.get(f"/api/v2/courses/{course['id']}/structure").json()["materials"][0]

    assert entry["material_id"] == upload.json()["id"]
    assert entry["concepts"] == 0 and entry["has_structure"] is False and entry["has_levels"] is False


# ---- dry-run：动手之前把影响说清楚 ----

def _attach_history(client: TestClient, course_id: str, *, mastery: list[str], mistakes: list[str]) -> None:
    tree = _concept_rows(client, course_id)
    with workspace(client).store.write() as conn:
        for name in mastery:
            conn.execute(
                "INSERT INTO concept_mastery(concept_id, course_id, bkt_p, fsrs_stability, fsrs_difficulty,"
                " objective_events, algorithm_version, updated_at) VALUES (?, ?, 0.5, 1.0, 5.0, 3, 'v1', 'now')",
                (tree[name][0], course_id))
        for index, name in enumerate(mistakes):
            conn.execute(
                "INSERT INTO mistake_records(id, course_id, concept_id, status, wrong_count, streak,"
                " first_wrong_at, last_wrong_at) VALUES (?, ?, ?, 'active', 2, 0, 'now', 'now')",
                (f"mistake_{index}", course_id, tree[name][0]))


def test_the_preview_predicts_exactly_what_the_rebuild_will_delete(client):
    """判据是真实的库变化，不是接口把预告原样回显一遍。"""
    course_id, material_id = _indexed_pdf(client)
    # SJF 与分页会被删掉且挂着历史；FIFO 也挂着历史但会留下来，用它挡住多算。
    _attach_history(client, course_id, mastery=["SJF", "FIFO"], mistakes=["分页"])
    storage = workspace(client).knowledge._repository.material_storage_path(material_id)
    storage.write_bytes(_outlined_pdf(TRIMMED_OUTLINE))
    before = set(_concept_rows(client, course_id))

    preview = client.post(f"/api/v2/materials/{material_id}/structure/preview")
    assert preview.status_code == 200, preview.text
    predicted = preview.json()
    applied = client.post(f"/api/v2/materials/{material_id}/structure")
    assert applied.status_code == 200, applied.text

    after = set(_concept_rows(client, course_id))
    assert predicted["removed"] == len(before - after) == 3
    assert predicted["added"] == len(after - before) == 0
    assert predicted["kept"] == len(before & after) == 2
    assert sorted(predicted["removed_names"]) == sorted(before - after)
    # 掌握度两条、错题一条，落在三个被删概念里的只有 SJF 与分页。
    assert predicted["at_risk"] == 2 and sorted(predicted["at_risk_names"]) == ["SJF", "分页"]


def test_the_preview_writes_nothing(client):
    course_id, material_id = _indexed_pdf(client)
    storage = workspace(client).knowledge._repository.material_storage_path(material_id)
    storage.write_bytes(_outlined_pdf(TRIMMED_OUTLINE))
    before = _concept_rows(client, course_id)

    preview = client.post(f"/api/v2/materials/{material_id}/structure/preview")

    assert preview.status_code == 200 and preview.json()["removed"] == 3
    assert _concept_rows(client, course_id) == before


def test_the_preview_reports_the_no_op_when_nothing_can_be_extracted(client):
    """抽不出候选时重建是空操作（现有概念一条都不删），预告要说得出这件事。"""
    course_id, material_id = _indexed_pdf(client)
    workspace(client).knowledge._concepts_for = lambda *_args, **_kwargs: []

    predicted = client.post(f"/api/v2/materials/{material_id}/structure/preview").json()

    assert predicted["empty"] is True
    assert predicted["removed"] == 0 and predicted["added"] == 0 and predicted["at_risk"] == 0
    assert len(_concept_rows(client, course_id)) == 5


def test_the_preview_does_not_credit_this_file_with_concepts_another_file_owns(client):
    """同名概念归第一次抽到它的那份教材，重建不会改归属。

    预告里「保留」必须是这份教材自己的数，否则用户看到「保留 2 个」，
    确认后紧挨着的状态行却写「0 个概念」。
    """
    course_id, _first = _indexed_pdf(client)
    second = _index_more(client, course_id, chapters=SHARED_OUTLINE)

    predicted = client.post(f"/api/v2/materials/{second}/structure/preview").json()
    before = _status_of(client, course_id, second)

    # 两个候选（调度、FIFO）的名字都归第一份教材，这一份自己什么都没有。
    assert predicted["candidates"] == 2
    assert predicted["kept"] == before["concepts"] == 0
    assert predicted["owned_elsewhere"] == 2
    assert predicted["added"] == 0 and predicted["removed"] == 0
    assert predicted["kept"] + predicted["added"] + predicted["owned_elsewhere"] == predicted["candidates"]
    # 删除也是教材级：第一份独有的概念不在这一份的预告里。
    assert predicted["removed_names"] == []
    # 层级同理只落在自己的行上，别让用户先被告知「这次能解析出层级」再看到相反的状态。
    assert predicted["has_levels"] is before["has_levels"] is False

    assert client.post(f"/api/v2/materials/{second}/structure").status_code == 200

    after = _status_of(client, course_id, second)
    assert after["concepts"] == predicted["kept"] + predicted["added"]
    assert after["has_levels"] is predicted["has_levels"]


def test_the_preview_merges_case_variants_the_same_way_the_rebuild_does(client):
    """预告与执行同源：候选先合并只差大小写的名字，再按派生 id 对账。

    少了这一步预告会把 Attention / ATTENTION 报成两个概念，执行只会落一行。
    """
    course_id, material_id = _indexed_pdf(client)
    workspace(client).knowledge._concepts_for = lambda *_args, **_kwargs: [
        {"name": "Attention", "page": 1, "mention_count": 3, "level": 0, "ordinal": 0},
        {"name": "ATTENTION", "page": 2, "mention_count": 1, "level": 0, "ordinal": 1},
    ]

    predicted = client.post(f"/api/v2/materials/{material_id}/structure/preview").json()
    assert client.post(f"/api/v2/materials/{material_id}/structure").status_code == 200

    assert predicted["candidates"] == 1 and predicted["added"] == 1
    assert set(_concept_rows(client, course_id)) == {"Attention"}


def test_rebuilding_only_replays_the_projections_when_a_concept_comes_back(client):
    """回填闸门是「概念又回来了」的补救开关。概念集合没变化时别整门课重置它，
    否则预告说「记录不受影响」而实际上闸门已经开了。"""
    course_id, material_id = _indexed_pdf(client)
    space = workspace(client)
    with space.store.write() as conn:
        conn.execute("INSERT INTO mistake_backfills(course_id, completed_at) VALUES (?, 'now')", (course_id,))

    def gate_open() -> bool:
        with space.store.read() as conn:
            return conn.execute("SELECT 1 FROM mistake_backfills WHERE course_id = ?", (course_id,)).fetchone() is None

    # 概念一个没变：闸门保持关着。
    assert client.post(f"/api/v2/materials/{material_id}/structure").json()["added"] == 0
    assert gate_open() is False

    # 概念回来了（先删掉一个，再重建）：投影必须重放，闸门要打开。
    with space.store.write() as conn:
        conn.execute("DELETE FROM concepts WHERE course_id = ? AND name = 'SJF'", (course_id,))
    assert client.post(f"/api/v2/materials/{material_id}/structure").json()["added"] == 1
    assert gate_open() is True


def test_rebuilding_the_structure_keeps_the_history_of_concepts_that_survive(client):
    """留下来的概念保住 id，掌握度与错题不断档——这是「别整批删了再插」的全部意义。"""
    course_id, material_id = _indexed_pdf(client)
    _attach_history(client, course_id, mastery=["FIFO"], mistakes=["FIFO"])
    fifo_id = _concept_rows(client, course_id)["FIFO"][0]

    assert client.post(f"/api/v2/materials/{material_id}/structure").status_code == 200

    with workspace(client).store.read() as conn:
        assert conn.execute("SELECT count(*) FROM concept_mastery WHERE concept_id = ?", (fifo_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM mistake_records WHERE concept_id = ?", (fifo_id,)).fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ---- 组合流水线的收尾与重启恢复 ----

def _interrupt(client: TestClient, material_id: str, stage: str) -> None:
    """把这份教材的索引作业按下在指定 stage 上，模拟进程在那一刻被杀。"""
    with workspace(client).store.write() as conn:
        conn.execute("UPDATE jobs SET status='running', stage=? WHERE material_id = ? AND type='index'",
                     (stage, material_id))


def test_a_restart_after_the_index_leg_finished_keeps_the_material_usable(client):
    """chunks 与向量都在，只是结构那一段没跑完。作业记成中断，教材不能跟着降级——
    降了就只能整份重索引（重新提取 + 重新向量化）才解得开。"""
    course_id, material_id = _indexed_pdf(client)
    client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
    chunks_before = _chunk_rows(client)
    _interrupt(client, material_id, "structure")

    workspace(client).knowledge._repository.recover_jobs_after_restart()

    assert _index_status(client, course_id, material_id) == "indexed"
    assert _chunk_rows(client) == chunks_before
    assert client.get(f"/api/v2/materials/{material_id}/wiki/estimate").status_code == 200
    assert client.post(f"/api/v2/materials/{material_id}/wiki").status_code in {200, 201, 202}
    with workspace(client).store.read() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE material_id = ? AND type='index'",
                            (material_id,)).fetchone()[0] == "failed"


def test_a_restart_before_the_index_leg_finished_still_fails_the_material(client):
    """向量化半途被打断时 chunks 是残缺的，教材照旧降级，用户得重新索引。"""
    course_id, material_id = _indexed_pdf(client)
    _interrupt(client, material_id, "embedding")

    workspace(client).knowledge._repository.recover_jobs_after_restart()

    assert _index_status(client, course_id, material_id) == "failed"


def test_the_pipeline_moves_on_only_when_the_index_leg_says_it_finished(client):
    """收尾判据对齐索引那一段自己报的 stage。拿 status 当哨兵是借来的：
    以后 _run_index 多一种 running 的返回，结构就会在半成品上跑。"""
    course_id, material_id = _indexed_pdf(client)
    knowledge = workspace(client).knowledge
    parsed: list[str] = []
    knowledge._parse_structure_quietly = lambda material: parsed.append(material.id)
    knowledge._run_index = lambda job, _material: knowledge._repository.update_job(
        job.id, status="running", stage="awaiting_user", progress=50)
    job = knowledge._repository.create_job(type="index", material_id=material_id, course_id=course_id)

    result = knowledge._run_upload_pipelines(job, knowledge._material_or_error(material_id))

    assert parsed == []
    assert result.status == "running" and result.stage == "awaiting_user"


def test_a_scanned_pdf_waits_for_the_ocr_bill_instead_of_parsing_the_outline(client):
    """停在等 OCR 确认时两条流水线都不该往下走，作业也不能被记成完成。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    knowledge = workspace(client).knowledge
    knowledge._is_scanned_pdf = lambda _material, _path: True
    parsed: list[str] = []
    knowledge._parse_structure_quietly = lambda material: parsed.append(material.id)
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("scan.pdf", _outlined_pdf(FULL_OUTLINE), "application/pdf")})
    job = client.post(f"/api/v2/materials/{upload.json()['id']}/index").json()

    finished = _await_job(client, job["id"])

    assert finished["status"] == "failed" and finished["stage"] == "needs_ocr"
    assert _index_status(client, course["id"], upload.json()["id"]) == "needs_ocr"
    assert parsed == [] and _concept_rows(client, course["id"]) == {}


def test_reparsing_survives_the_original_file_being_gone(client):
    """重算只吃已落库的正文。原文件被清掉时退回从正文刮标题，不该炸。"""
    course_id, material_id = _indexed_pdf(client)
    workspace(client).knowledge._repository.material_storage_path(material_id).unlink()

    response = client.post(f"/api/v2/materials/{material_id}/structure")

    assert response.status_code == 200, response.text
    assert _concept_rows(client, course_id)


# ---- 知识页：层级下发 + 构建前的成本预估 ----

def test_the_wiki_page_list_carries_the_hierarchy(client):
    """页面早就把 parent_id/level/order 写进了 frontmatter，服务层不能在这里把它丢掉。"""
    from modules.knowledge.wiki import WikiStore

    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})
    store = WikiStore(workspace(client).settings.data_dir)
    store.write(course_id=course["id"], concept_id="index", concept_name="课程总览", body="总览",
                source_hash="h", source_refs=[], updated_at="2026-08-01T00:00:00Z", level=0, order=-1)
    store.write(course_id=course["id"], concept_id="c_root", concept_name="调度", body="章",
                source_hash="h", source_refs=[], updated_at="2026-08-01T00:00:00Z", level=0, order=0)
    store.write(course_id=course["id"], concept_id="c_leaf", concept_name="FIFO", body="节",
                source_hash="h", source_refs=[], updated_at="2026-08-01T00:00:00Z",
                parent_id="c_root", level=1, order=1)

    pages = {page["concept_id"]: page for page in client.get(f"/api/v2/courses/{course['id']}/wiki").json()["pages"]}

    assert pages["c_leaf"]["parent_id"] == "c_root" and pages["c_leaf"]["level"] == 1
    assert pages["c_leaf"]["order"] == 1
    assert pages["c_root"]["parent_id"] == "" and pages["c_root"]["level"] == 0
    assert pages["index"]["order"] == -1


def test_the_wiki_estimate_counts_pages_without_calling_the_model(client):
    """花钱之前先给账单。这一步只跑 plan_sections，一次模型调用都不该发生。"""
    course_id, material_id = _indexed_pdf(client)
    client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})

    class Forbidden:
        def chat(self, **_kwargs):
            raise AssertionError("预估不许调模型")

    workspace(client).knowledge._responder = Forbidden()

    estimate = client.get(f"/api/v2/materials/{material_id}/wiki/estimate")

    assert estimate.status_code == 200, estimate.text
    payload = estimate.json()
    # 五个书签一节一页，外加一张课程首页；每页一次调用。
    assert payload["pages"] == 6 and payload["calls"] == 6
    assert payload["seconds"] > 0 and payload["minutes"] > 0
    assert payload["has_levels"] is True and payload["candidates"] == 5


def test_the_wiki_estimate_waits_for_the_index_to_finish(client):
    """正文已经落库、索引还没收工时也要挡住：概念目录那时还没写完，账单会算少。"""
    course_id, material_id = _indexed_pdf(client)
    client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
    workspace(client).knowledge._repository.set_material_status(material_id, "indexing")

    assert client.get(f"/api/v2/materials/{material_id}/wiki/estimate").status_code == 409


def test_the_wiki_estimate_needs_text_in_the_database(client):
    """状态写着 indexed 但正文没了（老库、手工清理过）：说清楚，别报出 1 页的空账单。"""
    course_id, material_id = _indexed_pdf(client)
    client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
    with workspace(client).store.write() as conn:
        conn.execute("DELETE FROM chunks WHERE material_id = ?", (material_id,))

    assert client.get(f"/api/v2/materials/{material_id}/wiki/estimate").status_code == 409


def test_the_wiki_estimate_is_refused_while_wiki_is_off(client):
    """课程没启用 Wiki 时连账单都不给算，和构建接口同一道闸门。"""
    _course_id, material_id = _indexed_pdf(client)

    assert client.get(f"/api/v2/materials/{material_id}/wiki/estimate").status_code == 409


def test_a_file_without_bookmarks_reports_no_hierarchy_in_both_the_preview_and_the_bill(client):
    """没有目录书签的教材解析不出层级。预告与账单都得说 False——界面靠这句提醒用户。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("notes.md", MARKDOWN_NOTES, "text/markdown")})
    material_id = upload.json()["id"]
    job = client.post(f"/api/v2/materials/{material_id}/index").json()
    assert _await_job(client, job["id"])["status"] == "completed"

    predicted = client.post(f"/api/v2/materials/{material_id}/structure/preview").json()
    estimate = client.get(f"/api/v2/materials/{material_id}/wiki/estimate").json()

    assert predicted["empty"] is False and predicted["candidates"] > 0
    assert predicted["has_levels"] is False
    assert estimate["has_levels"] is False
    assert _status_of(client, course["id"], material_id)["has_levels"] is False


def test_structure_endpoints_reject_an_unknown_material(client):
    assert client.post("/api/v2/materials/material_missing/structure").status_code == 404
    assert client.post("/api/v2/materials/material_missing/structure/preview").status_code == 404
    assert client.get("/api/v2/courses/course_missing/structure").status_code == 404
