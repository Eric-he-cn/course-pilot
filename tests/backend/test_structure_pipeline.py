"""目录结构解析从 RAG 索引流水线里拆出来，可以单独重算。

两条流水线共享「文本准备」（提取 → 切块 → 写 chunks），之后分叉：检索索引管向量与 FTS，
目录结构管概念与层级。所以想拿回层级不必重新向量化，也不必冒「概念被换掉、掌握度和错题
连带删除」的险——重算之前先给用户看一遍影响。
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


def test_the_wiki_estimate_needs_indexed_text(client):
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("book.pdf", _outlined_pdf(FULL_OUTLINE), "application/pdf")})

    assert client.get(f"/api/v2/materials/{upload.json()['id']}/wiki/estimate").status_code == 409


def test_structure_endpoints_reject_an_unknown_material(client):
    assert client.post("/api/v2/materials/material_missing/structure").status_code == 404
    assert client.post("/api/v2/materials/material_missing/structure/preview").status_code == 404
    assert client.get("/api/v2/courses/course_missing/structure").status_code == 404
