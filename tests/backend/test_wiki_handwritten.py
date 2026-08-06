"""知识页手写区的编辑入口：分隔线以下归用户，界面能读能写。

这一批守两件事——写手写区一个字节都不能碰生成区与 frontmatter；构建正在跑时拒绝写入，
因为构建会把整页重写一遍，交错保存必然丢更新。
"""
from __future__ import annotations

import time

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.knowledge import WikiDocument
from contracts.llm import ChatFinal
from core.settings import Settings
from modules.knowledge import wiki
from modules.knowledge.api import WikiBuildInProgressError, WikiPageTooLargeError
from modules.knowledge.wiki import HANDWRITTEN_MARKER, MAX_PAGE_BYTES, REBUILD_HEADROOM, WikiStore


def _final(text: str) -> ChatFinal:
    return ChatFinal(text=text, finish_reason="stop", provider="fake", model="fake", mode="chat")


def _store(tmp_path) -> WikiStore:
    store = WikiStore(tmp_path)
    store.write(course_id="c1", concept_id="cpt_1", concept_name="链式法则",
                body="先求外层导数，再乘内层导数。[p.3]", source_hash="hash_1",
                source_refs=["calculus.pdf p.3 #chunk_3"], updated_at="2026-08-06T00:00:00Z",
                material_id="m1")
    return store


def _path(store: WikiStore, concept_id: str = "cpt_1"):
    return store._locate(course_id="c1", concept_id=concept_id)


# ---------------------------------------------------------------- 落盘边界

def test_writing_the_handwritten_block_leaves_the_generated_half_byte_identical(tmp_path):
    """生成区与 frontmatter 是系统的账，用户写自己那一段不该动到它们一个字节。"""
    store = _store(tmp_path)
    before = _path(store).read_text(encoding="utf-8")
    generated = before.split(HANDWRITTEN_MARKER)[0]

    store.write_handwritten(course_id="c1", concept_id="cpt_1", text="老师说考试只考外层那一步。")

    after = _path(store).read_text(encoding="utf-8")
    assert after.split(HANDWRITTEN_MARKER)[0] == generated
    assert after.count(HANDWRITTEN_MARKER) == 1
    assert after.endswith("老师说考试只考外层那一步。\n")


def test_the_handwritten_block_comes_back_out_of_split_page(tmp_path):
    store = _store(tmp_path)
    store.write_handwritten(course_id="c1", concept_id="cpt_1", text="我自己的例子：sin(x²)。")

    page = wiki.split_page(concept_id="cpt_1", document=store.read(course_id="c1", concept_id="cpt_1"))

    assert page.handwritten == "我自己的例子：sin(x²)。"
    assert page.concept_name == "链式法则"
    assert HANDWRITTEN_MARKER not in page.body and "先求外层导数" in page.body


def test_saving_an_empty_text_clears_the_block_but_keeps_the_marker(tmp_path):
    """清空是合法操作。分隔线要留着，不然重新生成时那一页的手写区就没有落点了。"""
    store = _store(tmp_path)
    store.write_handwritten(course_id="c1", concept_id="cpt_1", text="先写一句。")

    store.write_handwritten(course_id="c1", concept_id="cpt_1", text="   \n  ")

    raw = store.read(course_id="c1", concept_id="cpt_1")
    assert raw.count(HANDWRITTEN_MARKER) == 1 and raw.endswith(HANDWRITTEN_MARKER + "\n")
    assert wiki.split_page(concept_id="cpt_1", document=raw).handwritten == ""


def test_a_page_without_a_marker_gets_one_and_keeps_its_existing_text(tmp_path):
    """老版本写的页没有分隔线。原有内容整段算生成区，不能被当成手写区替换掉。"""
    store = _store(tmp_path)
    path = _path(store)
    path.write_text(path.read_text(encoding="utf-8").split(HANDWRITTEN_MARKER)[0], encoding="utf-8")

    store.write_handwritten(course_id="c1", concept_id="cpt_1", text="补一句。")

    raw = path.read_text(encoding="utf-8")
    assert raw.count(HANDWRITTEN_MARKER) == 1
    assert "先求外层导数" in raw.split(HANDWRITTEN_MARKER)[0]
    assert wiki.split_page(concept_id="cpt_1", document=raw).handwritten == "补一句。"


def test_rebuilding_the_page_keeps_what_the_user_wrote_through_this_entry(tmp_path):
    """手写区的全部意义就是重新生成不覆盖，换成界面写进去的那条路也要成立。"""
    store = _store(tmp_path)
    store.write_handwritten(course_id="c1", concept_id="cpt_1", text="这里容易和乘积法则搞混。")

    store.write(course_id="c1", concept_id="cpt_1", concept_name="链式法则", body="重新生成的正文",
                source_hash="hash_2", source_refs=["calculus.pdf p.4 #chunk_4"],
                updated_at="2026-08-07T00:00:00Z", material_id="m1")

    page = wiki.split_page(concept_id="cpt_1", document=store.read(course_id="c1", concept_id="cpt_1"))
    assert page.body == "重新生成的正文" and page.handwritten == "这里容易和乘积法则搞混。"


def test_the_retrieval_text_carries_the_note_behind_its_own_label():
    """检索行的组法：概念名打头（引用摘要按这里切），手写区带标注跟在正文之后。"""
    document = WikiDocument("cpt_1", "链式法则", "先求外层导数。", "老师说只考外层那一步。")

    assert wiki.retrieval_content(document) == (
        "链式法则\n\n先求外层导数。\n\n" + wiki.HANDWRITTEN_LABEL + "\n老师说只考外层那一步。")


def test_a_label_inside_the_generated_half_is_dropped_so_the_note_stays_whole():
    """读的一端按第一处标注拆手写区。生成区里再冒出一处，拆点就提前到那里，
    真正的手写区会被当成生成区的尾巴一起截掉。"""
    document = WikiDocument("cpt_1", "链式法则",
                            f"先求外层导数。{wiki.HANDWRITTEN_LABEL} 模型抄了一句标注。",
                            "老师说只考外层那一步。")

    content = wiki.retrieval_content(document)

    assert content.count(wiki.HANDWRITTEN_LABEL) == 1
    assert content.partition(wiki.HANDWRITTEN_LABEL)[2].strip() == "老师说只考外层那一步。"
    assert "模型抄了一句标注。" in content, "只摘标注，生成区的字一个不少"


def test_the_retrieval_text_of_a_page_without_a_note_is_unchanged():
    """没写过手写区的页占绝大多数，它们的检索行不该多出一段空标注。"""
    assert wiki.retrieval_content(WikiDocument("cpt_1", "链式法则", "先求外层导数。", "")) \
        == "链式法则\n\n先求外层导数。"
    assert wiki.HANDWRITTEN_LABEL not in wiki.retrieval_content(
        WikiDocument("cpt_1", "链式法则", "先求外层导数。", "   \n "))


def test_an_unknown_page_is_a_lookup_error_not_a_new_file(tmp_path):
    store = _store(tmp_path)

    with pytest.raises(LookupError):
        store.write_handwritten(course_id="c1", concept_id="cpt_nope", text="随便写点。")

    assert store.list_pages(course_id="c1") and len(list((tmp_path / "wiki" / "c1").rglob("*.md"))) == 1


def test_an_oversized_block_is_refused_and_the_page_on_disk_is_untouched(tmp_path):
    """超限要在写盘之前拦住。写一半再报错等于用户按一次保存丢掉整页。"""
    store = _store(tmp_path)
    before = _path(store).read_text(encoding="utf-8")

    with pytest.raises(WikiPageTooLargeError) as error:
        store.write_handwritten(course_id="c1", concept_id="cpt_1", text="话" * MAX_PAGE_BYTES)

    assert "cpt_1" in str(error.value)
    assert _path(store).read_text(encoding="utf-8") == before


def test_the_block_cannot_fill_the_page_right_up_to_the_hard_limit(tmp_path):
    """顶到硬上限的页，下一次重建时生成区一变长就整页写不下——那时用户已经花过模型的钱。
    手写区必须给重建留出余量，这条守的是那段余量真的留住了。"""
    store = _store(tmp_path)
    head = len(_path(store).read_text(encoding="utf-8").split(HANDWRITTEN_MARKER)[0].encode()) \
        + len(HANDWRITTEN_MARKER.encode()) + 2

    with pytest.raises(WikiPageTooLargeError):
        store.write_handwritten(course_id="c1", concept_id="cpt_1", text="a" * (MAX_PAGE_BYTES - head))

    store.write_handwritten(course_id="c1", concept_id="cpt_1",
                            text="a" * (MAX_PAGE_BYTES - REBUILD_HEADROOM - head))
    assert _path(store).stat().st_size <= MAX_PAGE_BYTES - REBUILD_HEADROOM


def test_a_marker_typed_by_the_user_is_dropped_but_the_words_after_it_survive(tmp_path):
    """页里恒有一条分隔线是读页的前提。删标记不删字：用户打的内容一个字都不能悄悄吞掉。"""
    store = _store(tmp_path)

    store.write_handwritten(course_id="c1", concept_id="cpt_1",
                            text=f"前面一句。\n{HANDWRITTEN_MARKER}\n后面还有一句。")

    raw = store.read(course_id="c1", concept_id="cpt_1")
    assert raw.count(HANDWRITTEN_MARKER) == 1
    page = wiki.split_page(concept_id="cpt_1", document=raw)
    assert "前面一句。" in page.handwritten and "后面还有一句。" in page.handwritten
    assert "先求外层导数" in page.body


def test_compose_names_the_page_it_could_not_write(tmp_path):
    """构建路径的超限报错也要点出是哪一页，否则整次构建只留下一句「超过大小上限」。"""
    store = _store(tmp_path)

    with pytest.raises(WikiPageTooLargeError) as error:
        store.write(course_id="c1", concept_id="cpt_big", concept_name="超大页",
                    body="正" * MAX_PAGE_BYTES, source_hash="h", source_refs=[], updated_at="now")

    assert "cpt_big" in str(error.value)


def test_one_page_that_will_not_fit_does_not_take_down_the_whole_build(tmp_path):
    """一页落不了盘就整次构建 failed 的话，模型钱花了、别的页也没写，而且每次重建都会重复。"""
    store = _store(tmp_path)
    # 手写区写到重建余量的边上（这是允许的），再让模型这次多写出比余量还多的字。
    head = len(_path(store).read_text(encoding="utf-8").split(HANDWRITTEN_MARKER)[0].encode()) \
        + len(HANDWRITTEN_MARKER.encode()) + 2
    store.write_handwritten(course_id="c1", concept_id="cpt_1",
                            text="留" * ((MAX_PAGE_BYTES - REBUILD_HEADROOM - head) // 3))
    sections = [
        wiki.Section(id="cpt_1", name="链式法则", level=0, parent_id=None, first_page=1, last_page=1,
                     chunks=[{"id": "chunk_1", "page": 1, "content": "第一节原文。", "ordinal": 1}]),
        wiki.Section(id="cpt_2", name="乘积法则", level=0, parent_id=None, first_page=2, last_page=2,
                     chunks=[{"id": "chunk_2", "page": 2, "content": "第二节原文。", "ordinal": 2}]),
    ]

    counts = wiki.build_pages(course_id="c1", material_id="m1", document="calculus.pdf", sections=sections,
                              store=store, now="2026-08-07T00:00:00Z",
                              ask=lambda messages: _final("这次的正文长了很多。" * 2000 + "[p.1]"))

    assert counts["oversized"] == 1 and counts["written"] >= 1
    assert "oversized=1" in wiki.coverage_summary(counts)
    # 写不下的那一页保持旧版本，手写区照样在
    kept = wiki.split_page(concept_id="cpt_1", document=store.read(course_id="c1", concept_id="cpt_1"))
    assert kept.handwritten.startswith("留") and "先求外层导数" in kept.body
    assert "乘积法则" in store.read(course_id="c1", concept_id="cpt_2")


def test_a_page_that_is_not_utf8_says_so_instead_of_crashing(tmp_path):
    store = _store(tmp_path)
    _path(store).write_bytes("# 链式法则\n按 GBK 存过的一页\n".encode("gbk"))

    with pytest.raises(ValueError) as error:
        store.write_handwritten(course_id="c1", concept_id="cpt_1", text="补一句。")

    assert not isinstance(error.value, WikiPageTooLargeError) and "cpt_1" in str(error.value)


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def client(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data", database_path=tmp_path / "data" / "coursepilot.db",
        uploads_dir=tmp_path / "data" / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=60, chunk_overlap=10, top_k_results=6,
        material_max_bytes=10 * 1024 * 1024, background_job_workers=1, background_job_queue_capacity=4,
    )
    with TestClient(create_app(settings=settings)) as test_client:
        yield test_client


def _poll(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/v2/jobs/{job_id}").json()
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("任务没有进入终态")


@pytest.fixture
def built(client) -> dict:
    """一门课，一份教材，一棵已经建好的知识页。返回课程 id 与一页的 concept_id。"""
    course = client.post("/api/v2/courses", json={"name": "高等数学"}).json()
    material = client.post(
        f"/api/v2/courses/{course['id']}/materials",
        files={"file": ("calculus.md", "# 链式法则\n\n先求外层导数，再乘内层导数。\n", "text/markdown")},
    ).json()
    assert _poll(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])["status"] == "completed"
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})
    assert _poll(client, client.post(f"/api/v2/materials/{material['id']}/wiki").json()["id"])["status"] == "completed"
    pages = client.get(f"/api/v2/courses/{course['id']}/wiki").json()["pages"]
    leaf = next(page for page in pages if page["concept_id"] != wiki.INDEX_ID)
    return {"course_id": course["id"], "material_id": material["id"], "concept_id": leaf["concept_id"]}


def test_reading_a_page_gives_the_two_halves_apart_from_the_whole_file(client, built):
    """界面按 body/handwritten 分区渲染，分隔标记不该当正文上屏。content 保留给旧调用方。"""
    response = client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert {"concept_id", "content", "body", "handwritten"} <= set(payload)
    assert HANDWRITTEN_MARKER in payload["content"], "整页字段仍是落盘原样"
    assert HANDWRITTEN_MARKER not in payload["body"] and payload["handwritten"] == ""
    assert payload["body"] and not payload["body"].startswith("---")


def test_the_handwritten_route_round_trips_and_leaves_the_rest_alone(client, built):
    saved = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                       json={"text": "我自己的例子：sin(x²) 的导数。"})

    assert saved.status_code == 200, saved.text
    assert saved.json()["handwritten"] == "我自己的例子：sin(x²) 的导数。"
    reread = client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}").json()
    assert reread["handwritten"] == "我自己的例子：sin(x²) 的导数。"
    assert reread["body"] == saved.json()["body"]
    assert HANDWRITTEN_MARKER not in reread["body"]


def test_saving_twice_replaces_the_block_instead_of_stacking_it_up(client, built):
    url = f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten"
    client.put(url, json={"text": "第一版。"})

    client.put(url, json={"text": "第二版。"})

    payload = client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}").json()
    assert payload["handwritten"] == "第二版。" and "第一版" not in payload["content"]
    assert payload["content"].count(HANDWRITTEN_MARKER) == 1


def test_writing_to_an_unknown_page_is_404(client, built):
    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/cpt_nope/handwritten",
                          json={"text": "写点什么。"})

    assert response.status_code == 404, response.text


def test_a_body_without_the_text_field_is_rejected_instead_of_clearing_the_note(client, built):
    """漏传字段和「用户要清空」是两件事。默认空串会让一次写错的请求静默抹掉整段笔记。"""
    url = f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten"
    client.put(url, json={"text": "别弄丢我。"})

    assert client.put(url, json={}).status_code == 422
    assert client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}"
                      ).json()["handwritten"] == "别弄丢我。"
    # 显式清空仍然放行
    assert client.put(url, json={"text": ""}).status_code == 200


def test_writing_to_an_unknown_course_is_404(client):
    response = client.put("/api/v2/courses/course_nope/wiki/cpt_1/handwritten", json={"text": "x"})

    assert response.status_code == 404


def test_an_oversized_block_is_413_with_a_message_that_names_the_page(client, built):
    """整页超限要说清楚，不能 500——用户那一屏字全在请求体里，报得含糊他不知道丢没丢。"""
    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                          json={"text": "话" * MAX_PAGE_BYTES})

    assert response.status_code == 413, response.text
    assert response.json()["error"] == {"code": "wiki_page_too_large", "retryable": False,
                                        "message": response.json()["detail"]}
    assert built["concept_id"] in response.json()["detail"]
    assert client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}").json()["handwritten"] == ""


def test_saving_is_refused_while_a_build_is_pending_for_that_course(client, built):
    """构建会整页重写。这时候放行等于让用户刚写的那段被生成结果盖掉。"""
    repository = workspace(client).knowledge._repository
    repository.create_job(type="wiki", material_id=built["material_id"], course_id=built["course_id"])

    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                          json={"text": "构建期间写的。"})

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "wiki_build_running"
    assert client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}").json()["handwritten"] == ""


def test_a_build_pending_on_another_course_does_not_block_this_one(client, built):
    """闸门按课程算。别的课在构建时把整个应用锁住，用户会以为功能坏了。"""
    other = client.post("/api/v2/courses", json={"name": "线性代数"}).json()
    repository = workspace(client).knowledge._repository
    repository.create_job(type="wiki", material_id=built["material_id"], course_id=other["id"])

    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                          json={"text": "隔壁课在构建，这门课照写。"})

    assert response.status_code == 200, response.text


def test_a_finished_build_does_not_block_saving(client, built):
    """built 这个 fixture 自己刚跑完一次构建，完成态的作业不该继续挡着写入。"""
    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                          json={"text": "构建早就跑完了。"})

    assert response.status_code == 200, response.text


def test_the_service_raises_the_build_in_progress_error_by_type(client, built):
    """路由靠类型分流到 409。服务层直接抛这个类型，改了路由的 except 顺序这条会红。"""
    knowledge = workspace(client).knowledge
    knowledge._repository.create_job(type="wiki", material_id=built["material_id"], course_id=built["course_id"])

    with pytest.raises(WikiBuildInProgressError):
        knowledge.write_wiki_handwritten(course_id=built["course_id"], concept_id=built["concept_id"], text="x")


# ------------------------------------------------------------ 手写区进检索

def _wiki_row(client: TestClient, built: dict) -> str:
    """这一页在检索库里的那一行正文——模型日常问答看得见的就是它。"""
    rows = workspace(client).knowledge._repository.list_wiki_rows(course_id=built["course_id"])
    return next(row["content"] for row in rows if row["concept_id"] == built["concept_id"])


def test_saving_a_note_refreshes_the_retrieval_row_right_away(client, built):
    """写完就要能被检索到。只写文件不刷检索行，用户的纠偏得等下一次构建才对模型可见。

    这条同时守 embedder 为 None 的装机：demo 配置没有嵌入模型，刷新照样要走完。
    """
    assert wiki.HANDWRITTEN_LABEL not in _wiki_row(client, built)

    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                          json={"text": "老师说考试只考外层那一步。"})

    assert response.status_code == 200, response.text
    content = _wiki_row(client, built)
    assert "老师说考试只考外层那一步。" in content and wiki.HANDWRITTEN_LABEL in content


def test_clearing_the_note_takes_it_back_out_of_retrieval(client, built):
    """撤回也要即时生效：删掉的批注还留在检索库里，模型会照着一句用户已经否掉的话作答。"""
    url = f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten"
    client.put(url, json={"text": "老师说考试只考外层那一步。"})
    assert "老师说考试只考外层那一步。" in _wiki_row(client, built)

    assert client.put(url, json={"text": ""}).status_code == 200

    content = _wiki_row(client, built)
    assert "老师说考试只考外层那一步。" not in content and wiki.HANDWRITTEN_LABEL not in content


def test_a_broken_page_elsewhere_does_not_stop_this_one_from_being_indexed(client, built):
    """同一门课另一页坏掉时，这一页的批注照样要进检索库。
    返回 200 还不够——刷新静默失败时它同样是 200，而用户的批注一个字都没进去。"""
    store = workspace(client).knowledge._wiki
    path = store._locate(course_id=built["course_id"], concept_id=wiki.INDEX_ID)
    # frontmatter 的 concept_id 是 ASCII，这一页照样会被列出来，读正文时才炸
    path.write_bytes(path.read_text(encoding="utf-8").encode("gbk"))

    response = client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
                          json={"text": "隔壁页坏了，这一页照写。"})

    assert response.status_code == 200, response.text
    content = _wiki_row(client, built)
    assert "隔壁页坏了，这一页照写。" in content and wiki.HANDWRITTEN_LABEL in content


def _wiki_owners(client: TestClient, course_id: str) -> dict[str, str]:
    """检索行是挂在哪份教材名下的——删教材时按它级联，写错了会连带删掉别人的页。"""
    repository = workspace(client).knowledge._repository
    with repository._store.read() as conn:
        return {row["concept_id"]: row["material_id"] for row in conn.execute(
            "SELECT concept_id, material_id FROM chunks WHERE course_id = ? AND source_kind = 'wiki'",
            (course_id,))}


def _second_material(client: TestClient, built: dict) -> dict:
    """再加一份教材并给它建页。页文件在删教材时不会被清掉，正好用来验孤儿页。"""
    other = client.post(f"/api/v2/courses/{built['course_id']}/materials", files={
        "file": ("extra.md", "# 洛必达法则\n\n零比零型可以求导再求极限。\n", "text/markdown")}).json()
    assert _poll(client, client.post(f"/api/v2/materials/{other['id']}/index").json()["id"])["status"] == "completed"
    assert _poll(client, client.post(f"/api/v2/materials/{other['id']}/wiki").json()["id"])["status"] == "completed"
    return other


def test_saving_a_note_does_not_drag_a_deleted_materials_page_back_in(client, built):
    """端到端的那条线：删教材时它的检索行一并没了，页文件却留在原位（那是为了保用户手写）。
    存一次批注不能把这些页塞回检索库。判据本身在服务层，见 test_wiki_citations 里的整课刷新。"""
    other = _second_material(client, built)
    orphan = next(page.concept_id for page in workspace(client).knowledge._wiki.list_pages(
        course_id=built["course_id"]) if page.material_id == other["id"])
    assert orphan in _wiki_owners(client, built["course_id"])
    assert client.delete(f"/api/v2/materials/{other['id']}").status_code == 204

    client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
               json={"text": "这门课删掉了一份讲义。"})

    assert orphan not in _wiki_owners(client, built["course_id"])
    assert orphan in [page.concept_id for page in workspace(client).knowledge._wiki.list_pages(
        course_id=built["course_id"])], "页文件本身还要在，护栏保的是用户手写"


def test_saving_a_note_only_rewrites_that_one_retrieval_row(client, built):
    """保存手写区不该把整课重写一遍：每页读盘 + 整批嵌入，上百页时用户按一次保存要等好几秒。
    别的页连行 id 都不该变——变了说明那一行是重新插进去的。"""
    _second_material(client, built)
    repository = workspace(client).knowledge._repository
    with repository._store.read() as conn:
        before = {row["concept_id"]: (row["id"], row["content"]) for row in conn.execute(
            "SELECT id, concept_id, content FROM chunks WHERE course_id = ? AND source_kind = 'wiki'",
            (built["course_id"],))}

    client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
               json={"text": "只改了这一页。"})

    with repository._store.read() as conn:
        after = {row["concept_id"]: (row["id"], row["content"]) for row in conn.execute(
            "SELECT id, concept_id, content FROM chunks WHERE course_id = ? AND source_kind = 'wiki'",
            (built["course_id"],))}
    assert set(after) == set(before)
    changed = {cid for cid in after if after[cid] != before[cid]}
    assert changed == {built["concept_id"]}, "只有这一页该变"
    assert "只改了这一页。" in after[built["concept_id"]][1]


def test_the_refreshed_row_keeps_its_place_in_the_ordering(client, built):
    """单页替换要沿用原来的排序位置，否则每存一次批注这一页就往后跳一格。"""
    _second_material(client, built)
    repository = workspace(client).knowledge._repository

    def order() -> list[str]:
        return [row["concept_id"] for row in repository.list_wiki_rows(course_id=built["course_id"])]

    before = order()
    client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
               json={"text": "存一次。"})

    assert order() == before


def test_a_page_keeps_the_material_it_records_when_a_note_is_saved(client, built):
    """归属只由页自己记的那份决定。保存批注时按别的规则重挑一份，
    会把页悄悄改挂到别人名下——那份教材被删时这一页就跟着没了。"""
    _second_material(client, built)
    owners = _wiki_owners(client, built["course_id"])
    recorded = {page.concept_id: page.material_id for page
                in workspace(client).knowledge._wiki.list_pages(course_id=built["course_id"])}

    client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
               json={"text": "又传了一份讲义。"})

    after = _wiki_owners(client, built["course_id"])
    assert {cid: owner for cid, owner in after.items() if recorded[cid]} \
        == {cid: recorded[cid] for cid in after if recorded[cid]}
    assert after[built["concept_id"]] == owners[built["concept_id"]]


def test_a_rebuild_keeps_what_was_saved_through_the_route(client, built):
    """端到端的那条线：界面写进去的补充，重新构建一次之后还在。"""
    client.put(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}/handwritten",
               json={"text": "考点：只考外层那一步。"})

    assert _poll(client, client.post(f"/api/v2/materials/{built['material_id']}/wiki").json()["id"])["status"] == "completed"

    assert client.get(f"/api/v2/courses/{built['course_id']}/wiki/{built['concept_id']}"
                      ).json()["handwritten"] == "考点：只考外层那一步。"
