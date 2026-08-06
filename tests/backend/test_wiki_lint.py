"""知识页体检：零模型调用的确定性检查，只报不改。

最有价值的一条是「正文标的页码不在这页的出处里」——那正是幻觉引用的形状，
落盘之后没有任何东西会发现它。判据同时要守住反面：正常构建的产物一条 error 都不该报。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.llm import ChatFinal
from core.settings import Settings
from modules.knowledge import wiki
from test_wiki_traversal import (
    DEEP_LEARNING, NO_OUTLINE, _build, _env, _index_and_build, needs_deep_learning, needs_no_outline,
)


ROOT = Path(__file__).resolve().parents[2]
# 体检发现的 code 在源码里有三种写法：标注那几条由 _classify_mark 与等级一起返回，
# 逐页那几条走 report()，教材级对账走字典字面量。写法变了由下面的条数闸报出来。
_EMITTED_CODE = re.compile(r'"(\w+)", "(?:error|warn)"|report\(page, "(?:error|warn)", "(\w+)"|"code": "(\w+)"')
LINT_CODE_COUNT = 10


def _page(concept_id: str = "leaf", *, name: str = "小节", body: str = "正文。",
          refs: tuple[str, ...] = ("讲义.pdf p.1 #chunk_1",), parent_id: str = "",
          material_id: str = "m1", source_hash: str = "hash_1") -> wiki.LintPage:
    return wiki.LintPage(concept_id=concept_id, concept_name=name, body=body, refs=refs,
                         parent_id=parent_id, material_id=material_id, source_hash=source_hash)


def _lint(pages: list[wiki.LintPage], *, material_pages: dict[str, set[int]] | None = None,
          names: dict[str, str] | None = None) -> list[dict[str, object]]:
    return wiki.lint_pages(pages, material_pages={"m1": {1}} if material_pages is None else material_pages,
                           material_names={"m1": "讲义.pdf"} if names is None else names)


def _codes(issues: list[dict[str, object]], level: str) -> list[str]:
    return [str(issue["code"]) for issue in issues if issue["level"] == level]


def _inject(store, course_id: str, concept_id: str, text: str) -> None:
    """把一句话塞进这一页的正文，落在分隔线之前——分隔线之后是手写区，体检不看那半。"""
    path = store._locate(course_id=course_id, concept_id=concept_id)
    path.write_text(path.read_text(encoding="utf-8").replace(
        wiki.HANDWRITTEN_MARKER, f"{text}\n\n{wiki.HANDWRITTEN_MARKER}", 1), encoding="utf-8")


# ---- error：用户该重建或该修的 ----

def test_a_leaf_citing_a_page_it_never_read_is_an_error():
    """幻觉引用的形状：这一页只读了 p.1，正文却把结论标到了 p.9。"""
    invented = _page(body="批量规范化在训练时按小批量统计 [讲义.pdf p.9]。")
    grounded = _page(body="批量规范化在训练时按小批量统计 [讲义.pdf p.1]。")

    assert _codes(_lint([invented]), "error") == ["page_out_of_range"]
    assert _lint([grounded]) == []


def test_the_out_of_range_report_carries_the_page_numbers():
    """报告是接口：光说「有问题」用户无从下手，越界的是哪几页要写进去。"""
    issue = _lint([_page(body="甲 [p.7]。乙 [p.9]。丙 [p.7]。")])[0]

    assert issue["code"] == "page_out_of_range" and issue["level"] == "error"
    assert issue["pages"] == [7, 9] and issue["n"] == 2
    assert issue["concept_id"] == "leaf" and issue["concept_name"] == "小节"


def test_a_bare_page_mark_is_checked_the_same_way():
    """`[p.9]` 与 `[讲义.pdf p.9]` 是同一件事，前端两种都接。"""
    assert _codes(_lint([_page(body="结论 [p.9]。")]), "error") == ["page_out_of_range"]
    assert _lint([_page(body="结论 [p.1]。")]) == []


def test_an_overview_page_must_not_cite_textbook_pages_at_all():
    """中间页读的是子页不是原文，页码它无从核对，提示词也明确禁止——标了就是编的。"""
    faulty = [_page("chapter", body="这一章分成三节 [讲义.pdf p.1]。", refs=("子页 甲 <leaf>",)),
              _page("leaf", parent_id="chapter")]
    fixed = [_page("chapter", body="这一章分成三节，见「甲」。", refs=("子页 甲 <leaf>",)),
             _page("leaf", parent_id="chapter")]

    assert _codes(_lint(faulty), "error") == ["overview_cites_pages"]
    assert _lint(fixed) == []


def test_an_overview_mark_is_reported_once_not_twice():
    """同一处写法同时撞上「总览页不许标页码」与「这个资料名不存在」，只报前一条。"""
    pages = [_page("chapter", body="这一章见 [编造的书.pdf p.3]。", refs=("子页 甲 <leaf>",)),
             _page("leaf", parent_id="chapter")]

    assert [issue["code"] for issue in _lint(pages)] == ["overview_cites_pages"]


def test_the_course_home_page_counts_as_an_overview_even_with_no_children():
    """首页没有子页，反查父子关系判不出它是总览页，得按 id 认。"""
    index = wiki.LintPage(concept_id=wiki.INDEX_ID, concept_name="课程总览",
                          body="这门课分成三部分 [讲义.pdf p.1]。", refs=("顶层页 甲",),
                          source_hash="hash_index")

    assert _codes(_lint([index], material_pages={}), "error") == ["overview_cites_pages"]


def test_a_document_name_that_belongs_to_no_material_is_an_error():
    """页码对得上也可能是编的：模型把出处安到一份这门课根本没有的资料上。"""
    page = _page(body="结论 [补充讲义.pdf p.1]。")

    assert _codes(_lint([page]), "error") == ["fabricated_document"]


def test_citing_another_material_of_the_same_course_is_only_a_warning():
    """这门课确实有这份教材，只是这一页没读过它。可能是对的，不能按编造报。"""
    page = _page(body="结论 [补充讲义.pdf p.1]。")

    issues = _lint([page], names={"m1": "讲义.pdf", "m2": "补充讲义.pdf"})
    assert [(issue["level"], issue["code"]) for issue in issues] == [("warn", "cross_document_mark")]
    assert issues[0]["documents"] == ["补充讲义.pdf"]


def test_a_document_level_mark_is_checked_by_name_but_only_warns():
    """没有页码的整份文档标注常常只是行文里提了个文件名，比编造页码轻一档。"""
    assert _codes(_lint([_page(body="配置写在 [README.md] 里。")]), "error") == []
    assert _codes(_lint([_page(body="配置写在 [README.md] 里。")]), "warn") == ["fabricated_document"]
    assert _lint([_page(body="结论 [讲义.pdf]。")]) == []


def test_only_names_that_look_like_files_are_checked():
    """「第三章」不是文件名，「讲义.pdf p.1,」是正则从多页标注里切歪的半截——
    这两种都判不出结论，判了就是假 error。"""
    assert _lint([_page(body="见 [第三章 p.1]。")]) == []
    assert _lint([_page(body="见 [讲义.pdf p.1, p.1]。")]) == []


def test_a_leaf_with_no_parsable_source_is_an_error():
    """叶子页零出处等于整页没有依据，而中间页的出处本来就是子页，不能一起报。"""
    lonely = _page(refs=())
    branch = [_page("chapter", refs=("子页 甲 <leaf>",)), _page("leaf", parent_id="chapter")]

    assert _codes(_lint([lonely], material_pages={}), "error") == ["leaf_without_sources"]
    assert _lint(branch) == []


def test_a_source_pointing_at_a_page_the_textbook_does_not_have_is_an_error():
    """出处与检索库对账。教材只有两页，出处却指到第七页——同名教材串页就是这个形状。"""
    page = _page(refs=("讲义.pdf p.7 #chunk_7",))

    dangling = _lint([page], material_pages={"m1": {1, 2}})
    assert _codes(dangling, "error") == ["dangling_page_refs"]
    assert dangling[0]["pages"] == [7] and dangling[0]["concept_name"] == "讲义.pdf"
    assert _lint([page], material_pages={"m1": {7}}) == []


# ---- warn：提示性的 ----

def test_pages_nobody_read_are_only_a_warning():
    """上级页接管区间、空白页不进检索库都可能让它响，所以不上升到 error。"""
    issues = _lint([_page()], material_pages={"m1": {1, 2, 3}})

    assert _codes(issues, "error") == [] and _codes(issues, "warn") == ["unread_pages"]
    assert issues[0]["pages"] == [2, 3] and issues[0]["n"] == 2


def test_a_material_without_any_wiki_page_is_not_reconciled():
    """没生成过页的教材两边必然对不上，那不是缺陷——按整本书报「没人读」等于噪音。"""
    assert _lint([], material_pages={"m1": {1, 2, 3}}) == []


def test_a_material_that_lost_its_text_is_not_reported_as_dangling():
    """教材删掉或重建成空之后检索库里一页都没有。把整本书报成悬空页码同样是噪音，
    这时该说的是「这份教材没了」，而那件事由构建时的清理负责。"""
    page = _page(refs=("讲义.pdf p.1 #chunk_1", "讲义.pdf p.2 #chunk_2"))

    assert _lint([page], material_pages={}) == []


def test_a_page_whose_parent_is_gone_is_a_warning():
    """prune 遇到手写区非空的页会故意留下孤儿，那正是要报给用户看的。"""
    orphan = _page("child", parent_id="ghost")

    assert _codes(_lint([orphan]), "warn") == ["orphan_page"]
    assert _lint([_page("chapter", refs=("子页 甲 <child>",)), _page("child", parent_id="chapter")]) == []


def test_an_empty_body_is_a_warning():
    assert _codes(_lint([_page(body="   \n")]), "warn") == ["empty_body"]


def test_a_page_without_a_source_hash_is_a_warning():
    """指纹缺了，增量刷新永远不会 skip 它，每次重建都要多花一次调用。"""
    assert _codes(_lint([_page(source_hash="")]), "warn") == ["no_source_hash"]
    assert _lint([_page(source_hash="hash_1")]) == []


# ---- 不该报的：误报会让整份报告没人看 ----

def test_page_marks_inside_code_are_left_alone():
    """围栏、行内、四空格缩进三种代码写法前端都不接原文，体检跟着同一条口径。"""
    fenced = _page(body="示例：\n\n```\nprint('[p.9]')\n```\n\n行内 `[p.9]` 同理。")
    indented = _page(body="示例：\n\n    print('[p.9]')\n\n以上。")

    assert _lint([fenced]) == []
    assert _lint([indented]) == []


def test_a_mark_in_an_indented_list_item_is_still_checked():
    """缩进的列表在前端渲染成 li，标注照样可点——不能跟缩进代码块一起放过。"""
    nested = _page(body="要点：\n\n- 一级\n    - 二级见 [p.9]\n")

    assert _codes(_lint([nested]), "error") == ["page_out_of_range"]


def test_a_document_name_only_written_in_this_page_refs_is_accepted():
    """教材可能已经从课程里删掉，页还在。出处行自己记着的名字不算编造。"""
    page = _page(body="结论 [旧讲义.pdf p.1]。", refs=("旧讲义.pdf p.1 #chunk_1",))

    assert _lint([page], names={}) == []


def test_the_report_puts_errors_first():
    """提示先出现的排法会让 error 被埋在下面，所以顺序要反过来。"""
    pages = [_page("a", source_hash=""), _page("b", body="结论 [p.9]。")]

    assert [issue["code"] for issue in _lint(pages)] == ["page_out_of_range", "no_source_hash"]


def test_every_lint_code_has_a_message_in_both_languages():
    """文案按 `library.lint_<code>` 动态查字典，TypeScript 管不到这条路：加了规则忘了加翻译，
    界面上直接显示 code 本身。条数闸挡住「正则一个都没匹配到，判据恒绿」。"""
    codes = {next(filter(None, groups)) for groups in
             _EMITTED_CODE.findall((ROOT / "backend/modules/knowledge/wiki.py").read_text(encoding="utf-8"))}
    assert len(codes) == LINT_CODE_COUNT, f"规则数变成了 {len(codes)}（{sorted(codes)}），文案与这条判据要一起跟进"

    halves = (ROOT / "frontend/src/i18n.ts").read_text(encoding="utf-8").split("const en: Dictionary = {")
    assert len(halves) == 2, "字典结构变了，这条判据要跟进"
    for half, language in zip(halves, ("zh", "en")):
        assert not [code for code in sorted(codes) if f"'library.lint_{code}':" not in half], \
            f"{language} 字典缺 {[code for code in sorted(codes) if f'library.lint_{code}' not in half]}"


# ---- 真实构建产物：规则不能对正常的页误报 ----

class CitingResponder:
    """把证据标签原样标进正文，模拟模型照抄【p.N】。用来验「合法标注不报」。"""

    def chat(self, *, messages, tools=()):
        label = re.search(r"【([^】\n]+)】", messages[-1].content)
        mark = f" [{label.group(1)}]" if label else ""
        yield ChatFinal(text=f"标题：本段小节\n\n这一段按给定内容写成的正文{mark}。",
                        finish_reason="stop", provider="stub", model="stub", mode="stub")


@needs_deep_learning
def test_a_clean_build_reports_no_errors(tmp_path):
    """最重要的一条判据：规则不能对正常构建产物误报。"""
    built = _build(tmp_path, DEEP_LEARNING)

    issues = built.service.wiki_lint(course_id=built.course_id)

    assert _codes(issues, "error") == [], issues


@needs_deep_learning
def test_legal_page_marks_in_a_real_build_are_not_reported(tmp_path):
    """上一条的正文不带页码标注，等于没走到规则 1。这条让每页都带一个合法标注。"""
    course, service, worker, store, _responder = _env(tmp_path)
    service._responder = CitingResponder()
    try:
        _index_and_build(service, worker, course_id=course.id, filename=DEEP_LEARNING.name,
                         mime_type="application/pdf", content=DEEP_LEARNING.read_bytes())
    finally:
        worker.shutdown()

    marked = [page.concept_id for page in store.list_pages(course_id=course.id)
              if "p." in wiki.split_page(concept_id=page.concept_id,
                                         document=store.read(course_id=course.id, concept_id=page.concept_id)).body]
    assert marked, "这条判据靠正文里真有页码标注才成立"
    assert _codes(service.wiki_lint(course_id=course.id), "error") == []


@needs_deep_learning
def test_the_handwritten_area_is_not_linted(tmp_path):
    """分隔线以下是用户自己写的，他写 [p.999] 不是缺陷。"""
    built = _build(tmp_path, DEEP_LEARNING)
    target = next(page.concept_id for page in built.store.list_pages(course_id=built.course_id)
                  if page.concept_id != wiki.INDEX_ID)
    path = built.store._locate(course_id=built.course_id, concept_id=target)
    path.write_text(path.read_text(encoding="utf-8") + "\n我自己记的：见 [不存在.pdf p.999]。\n",
                    encoding="utf-8")

    assert _codes(built.service.wiki_lint(course_id=built.course_id), "error") == []


@needs_deep_learning
def test_a_forged_page_mark_in_a_real_build_is_caught(tmp_path):
    """同一份真实产物，只在一页叶子的正文里塞一个越界页码，体检要报出来并点到那一页。"""
    built = _build(tmp_path, DEEP_LEARNING)
    pages = built.store.list_pages(course_id=built.course_id)
    branches = {page.parent_id for page in pages}
    target = next(page for page in pages
                  if page.concept_id != wiki.INDEX_ID and page.concept_id not in branches)
    _inject(built.store, built.course_id, target.concept_id, "补一句 [p.998]。")

    issues = built.service.wiki_lint(course_id=built.course_id)

    assert [(issue["concept_id"], issue["code"]) for issue in issues if issue["level"] == "error"] \
        == [(target.concept_id, "page_out_of_range")]
    assert issues[0]["pages"] == [998]


@needs_deep_learning
def test_the_build_summary_carries_the_issue_count(tmp_path):
    built = _build(tmp_path, DEEP_LEARNING)

    assert " issues=" in str(built.job.error_message), built.job.error_message


# ---- 接线：服务层真的把库里的数据喂给了判据 ----

@needs_deep_learning
def test_losing_textbook_pages_from_the_index_shows_up_as_dangling(tmp_path):
    """出处对账的基准是 chunks 表。服务层不去查它（或查错课程），这条判据就红——
    纯函数那侧的判据用手造数据，喂错了看不出来。"""
    built = _build(tmp_path, DEEP_LEARNING)
    assert built.service.wiki_lint(course_id=built.course_id) == []

    repository = built.service._repository
    with repository._store.write() as conn:
        conn.execute("DELETE FROM chunks WHERE material_id = ? AND source_kind = 'chunk' AND page >= 5",
                     (built.material_id,))

    issues = built.service.wiki_lint(course_id=built.course_id)
    assert [issue["code"] for issue in issues if issue["level"] == "error"] == ["dangling_page_refs"]
    assert issues[0]["concept_name"] == DEEP_LEARNING.name


@needs_deep_learning
@needs_no_outline
def test_a_mark_naming_another_material_of_the_course_is_not_called_fabricated(tmp_path):
    """教材名单也是服务层查库喂进去的。喂空了，引隔壁教材会被当成编造出处报 error。"""
    course, service, worker, store, _responder = _env(tmp_path)
    try:
        first, _job = _index_and_build(service, worker, course_id=course.id, filename=DEEP_LEARNING.name,
                                       mime_type="application/pdf", content=DEEP_LEARNING.read_bytes())
        _index_and_build(service, worker, course_id=course.id, filename=NO_OUTLINE.name,
                         mime_type="application/pdf", content=NO_OUTLINE.read_bytes())
    finally:
        worker.shutdown()
    target = next(page for page in store.list_pages(course_id=course.id)
                  if page.material_id == first.id and page.concept_id not in
                  {other.parent_id for other in store.list_pages(course_id=course.id)})
    _inject(store, course.id, target.concept_id,
            f"另见 [{NO_OUTLINE.name} p.1]，以及 [不存在教材.pdf p.1]。")

    issues = [issue for issue in service.wiki_lint(course_id=course.id)
              if issue["concept_id"] == target.concept_id]

    assert [(issue["level"], issue["code"]) for issue in issues] == [
        ("error", "fabricated_document"), ("warn", "cross_document_mark")]
    assert issues[0]["documents"] == ["不存在教材.pdf"] and issues[1]["documents"] == [NO_OUTLINE.name]


@needs_deep_learning
def test_a_page_that_cannot_be_read_does_not_sink_the_report(tmp_path):
    """按 GBK 存过的页解不出 UTF-8。一页坏文件不该让整门课的报告整个拿不出来，
    它自己会被「正文是空的」那条报出来。"""
    built = _build(tmp_path, DEEP_LEARNING)
    target = next(page for page in built.store.list_pages(course_id=built.course_id)
                  if page.concept_id != wiki.INDEX_ID)
    path = built.store._locate(course_id=built.course_id, concept_id=target.concept_id)
    path.write_bytes(path.read_bytes() + b"\xff\xfe")

    issues = built.service.wiki_lint(course_id=built.course_id)

    assert "empty_body" in [issue["code"] for issue in issues
                            if issue["concept_id"] == target.concept_id], issues


@needs_deep_learning
def test_a_failing_check_does_not_fail_a_finished_build(tmp_path):
    """页已经写完了，体检是旁路诊断。它挂了要静默降级，而且不能报 issues=0——
    那是「查过、没问题」的结论。"""
    course, service, worker, _store, _responder = _env(tmp_path)

    def explode(**_kwargs):
        raise RuntimeError("体检炸了")

    service.wiki_lint = explode
    try:
        _material, job = _index_and_build(service, worker, course_id=course.id, filename=DEEP_LEARNING.name,
                                          mime_type="application/pdf", content=DEEP_LEARNING.read_bytes())
    finally:
        worker.shutdown()

    assert job.status == "completed" and job.stage == "wiki_completed"
    assert "issues=" not in str(job.error_message), job.error_message


# ---- HTTP ----

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


def test_the_lint_route_returns_findings_for_a_real_course(client):
    """路由要排在 /wiki/{concept_id} 前面，否则 lint 会被当成概念 id 查页，静默 404。
    先塞一个缺陷再断言：干净的课返回空表，光查形状这条判据会空过。"""
    course = client.post("/api/v2/courses", json={"name": "高等数学"}).json()
    upload = client.post(f"/api/v2/courses/{course['id']}/materials",
                         files={"file": ("calculus.md", "# 链式法则\n\n先求外层导数，再乘内层导数。\n", "text/markdown")})
    material = upload.json()
    assert _poll(client, client.post(f"/api/v2/materials/{material['id']}/index").json()["id"])["status"] == "completed"
    client.patch(f"/api/v2/courses/{course['id']}", json={"wiki_enabled": True})
    assert _poll(client, client.post(f"/api/v2/materials/{material['id']}/wiki").json()["id"])["status"] == "completed"
    store = workspace(client).knowledge._wiki
    target = next(page for page in store.list_pages(course_id=course["id"]) if page.concept_id != wiki.INDEX_ID)
    _inject(store, course["id"], target.concept_id, "补一句 [calculus.md p.998]。")

    response = client.get(f"/api/v2/courses/{course['id']}/wiki/lint")

    assert response.status_code == 200, response.text
    issues = response.json()["issues"]
    assert [issue["code"] for issue in issues if issue["level"] == "error"] == ["page_out_of_range"], issues
    assert all({"concept_id", "concept_name", "level", "code"} <= set(issue) for issue in issues), issues
    assert all(issue["level"] in {"error", "warn"} for issue in issues), issues
    assert issues[0]["concept_id"] == target.concept_id and issues[0]["pages"] == [998]


def test_the_lint_route_is_empty_for_a_course_without_pages(client):
    course = client.post("/api/v2/courses", json={"name": "大学物理"}).json()

    assert client.get(f"/api/v2/courses/{course['id']}/wiki/lint").json() == {"issues": []}


def test_the_lint_route_404s_for_an_unknown_course(client):
    assert client.get("/api/v2/courses/course_nope/wiki/lint").status_code == 404
