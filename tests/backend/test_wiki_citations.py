"""知识页成为第三类可引用来源。

在这之前知识页只能读、不能引：模型越用它，回答里就越多结论没有出处可点。
这一批守三件事——知识页进检索库并有自己的固定名额、教材席位一条不少、
引用条目分得清哪条是转述哪条是原文。
"""
from __future__ import annotations

import json

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.knowledge import (
    Citation, KnowledgeHit, KnowledgeSearchPort, ResolvedKnowledgeScope, WikiDocument, WikiSource, WikiSources,
)
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest
from core.settings import Settings
from core.store import SQLiteStore
from modules.agent.tools import (
    MAIN,
    MAIN_PROFILE,
    SEARCH_LIMIT,
    WIKI_SEARCH_LIMIT,
    CitationRegistry,
    ToolExecutor,
)
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge import wiki
from modules.knowledge.wiki import WikiStore
from test_agent_loop import ScriptedChat, _events, _indexed_course_session, _settings

SCOPE = ResolvedKnowledgeScope(turn_id="turn_now", course_id="c1", resolver_version="v1")


def _material_hit(index: int) -> KnowledgeHit:
    return KnowledgeHit(
        citation=Citation(material_id="m1", document="os.pdf", page=index, chunk_id=f"chunk_{index}",
                          snippet=f"原文片段 {index}", score=1.0 - index / 100),
        content=f"教材原文第 {index} 段：调度算法。",
    )


def _wiki_hit(concept_id: str, name: str) -> KnowledgeHit:
    return KnowledgeHit(
        citation=Citation(material_id="", document=name, page=None, chunk_id=f"wiki:{concept_id}",
                          snippet=f"{name} 的整理稿开头", score=0.9, kind="wiki",
                          concept_id=concept_id, concept_name=name),
        content=f"# {name}\n\n这门课分成三部分：进程、内存、文件系统。",
    )


class FakeKnowledge:
    """两路检索各自可控，好把「名额」这件事单独量出来。"""

    def __init__(self, *, materials: int = SEARCH_LIMIT, wiki: int = WIKI_SEARCH_LIMIT) -> None:
        self._materials = [_material_hit(i) for i in range(1, materials + 1)]
        self._wiki = [_wiki_hit(f"cpt_{i}", f"概念{i}") for i in range(1, wiki + 1)]
        self.limits: dict[str, int] = {}
        self.source_calls: list[str] = []

    def search(self, *, scope, query, limit=6):
        self.limits["material"] = limit
        return self._materials[:limit]

    def search_wiki(self, *, scope, query, limit=2):
        self.limits["wiki"] = limit
        return self._wiki[:limit]

    def wiki_enabled(self, *, scope):
        return True

    def wiki_read(self, *, scope, concept_id):
        return WikiDocument(concept_id, "护航效应", "一句话定义：长作业拖住短作业。", "")

    def wiki_sources(self, *, scope, concept_id):
        self.source_calls.append(concept_id)
        return WikiSources((WikiSource("os.pdf", 9, "chunk_src_9", "第 9 页原文：护航效应。"),
                            WikiSource("os.pdf", 11, "chunk_src_11", "第 11 页原文：STCF。")), 5)


def _run(knowledge, name: str, arguments: str = "{}", registry: CitationRegistry | None = None):
    executor = ToolExecutor(
        knowledge=knowledge, plans=None, plan_writer=None, archive=None, evidence=None,
        artifacts=None, skills=None, memory=None,
    )
    return executor.execute(
        scope=SCOPE, session_id="s1", name=name, arguments=arguments,
        registry=registry or CitationRegistry(), allowed=MAIN_PROFILE,
        capabilities=MAIN.capabilities, budget=MAIN.per_tool_budget,
    )


# ---------------------------------------------------------------- 固定名额

def test_one_search_covers_both_pools_with_separate_quotas():
    """一次检索同时覆盖教材与知识页，两边各按自己的名额取，不放进同一个列表比相似度。"""
    knowledge = FakeKnowledge()
    registry = CitationRegistry()
    result = _run(knowledge, "search_materials", '{"query": "这门课分成哪几部分"}', registry)

    assert knowledge.limits == {"material": SEARCH_LIMIT, "wiki": WIKI_SEARCH_LIMIT}
    kinds = [item["kind"] for item in registry.citations]
    assert kinds.count("material") == SEARCH_LIMIT
    assert kinds.count("wiki") == WIKI_SEARCH_LIMIT


def test_material_seats_survive_when_the_wiki_pool_is_large():
    """知识页用概括的语言写，提问也是概括的语言：合排会把教材原文挤出去。
    名额固定，所以知识页再多，教材也还是那 6 条。"""
    registry = CitationRegistry()
    _run(FakeKnowledge(materials=SEARCH_LIMIT, wiki=30), "search_materials", '{"query": "调度"}', registry)

    materials = [item for item in registry.citations if item["kind"] == "material"]
    assert len(materials) == SEARCH_LIMIT
    assert [item["chunk_id"] for item in materials] == [f"chunk_{i}" for i in range(1, SEARCH_LIMIT + 1)]


def test_a_wiki_only_hit_still_counts_as_a_hit():
    """教材一条都没召回、知识页有：这一轮不能报「未命中」，否则模型手上明明有料却说没有。"""
    result = _run(FakeKnowledge(materials=0, wiki=1), "search_materials", '{"query": "整体结构"}')

    assert result.ok and "未匹配" not in result.text
    assert "概念1" in result.text


# ---------------------------------------------------------------- 引用体系

def test_wiki_hits_are_registered_as_their_own_kind():
    """引用条目要能一眼看出是转述：kind=wiki、没有页码、带概念名。"""
    registry = CitationRegistry()
    _run(FakeKnowledge(materials=0, wiki=1), "search_materials", '{"query": "整体结构"}', registry)

    entry = registry.citations[0]
    assert entry["kind"] == "wiki"
    assert entry["concept_id"] == "cpt_1" and entry["concept_name"] == "概念1"
    assert entry.get("page") is None and not entry.get("document")


def test_wiki_pages_dedupe_by_concept_not_by_chunk():
    """同一页被检索到又被 wiki_read 读一遍，是同一条来源，不该编两个号。"""
    registry = CitationRegistry()
    first, is_new = registry.register_wiki(concept_id="cpt_1", concept_name="护航效应", snippet="一句话")
    second, again = registry.register_wiki(concept_id="cpt_1", concept_name="护航效应", snippet="别的摘要")

    assert (first, is_new) == (1, True) and (second, again) == (1, False)
    assert len(registry.citations) == 1


def test_wiki_read_content_can_be_cited():
    """这一期改掉的契约：读回来的知识页也要能标引用，否则缺口只补了一半。"""
    registry = CitationRegistry()
    result = _run(FakeKnowledge(), "wiki_read", '{"concept_id": "cpt_convoy"}', registry)

    assert result.ok
    assert registry.citations and registry.citations[0]["kind"] == "wiki"
    assert result.new_citations == registry.citations
    # 编号要出现在正文里，模型才知道该标几号。
    assert f"[{registry.citations[0]['number']}]" in result.text


def test_the_page_still_says_it_is_not_the_textbook():
    """能引不等于等同教材。工具描述与正文头部都要点明它是转述、没有页码，
    引用条目会标成知识页——只写在描述里，模型读完长正文就只记得内容。"""
    from modules.agent.tools import TOOL_SPECS

    description = next(spec.description for spec in TOOL_SPECS if spec.name == "wiki_read")
    body = _run(FakeKnowledge(), "wiki_read", '{"concept_id": "cpt_1"}').text

    for text in (description, body):
        assert "不是教材原文" in text and "没有页码" in text
        assert "知识页" in text


# ------------------------------------------------------- 转述带着自己的教材出处

def test_a_cited_wiki_page_carries_the_pages_it_was_written_from():
    """知识页引用要带上这一页依据的教材页。模型读完转述就不再回教材，
    这几页是回答唯一还能追回原文的路。"""
    registry = CitationRegistry()
    _run(FakeKnowledge(), "wiki_read", '{"concept_id": "cpt_convoy"}', registry)

    entry = registry.citations[0]
    assert [(item["document"], item["page"]) for item in entry["sources"]] == [("os.pdf", 9), ("os.pdf", 11)]
    assert entry["sources"][0]["chunk_id"] and entry["sources"][0]["snippet"]
    assert entry["source_pages"] == 5  # 截断了也要说得出总共几页


def test_the_seed_search_path_carries_them_too():
    """一半的知识页是种子检索自动端上来的，那条路上不带出处等于只补了一半。"""
    knowledge = FakeKnowledge(materials=0, wiki=1)
    registry = CitationRegistry()
    _run(knowledge, "search_materials", '{"query": "整体结构"}', registry)

    assert knowledge.source_calls == ["cpt_1"]
    assert registry.citations[0]["sources"]


def test_the_pages_behind_a_wiki_citation_are_not_extra_numbered_sources():
    """出处挂在知识页那一条底下，不另编号：模型没读过那几页，标 [n] 就是没有依据的引用。
    教材那几个席位也因此一条不少。"""
    registry = CitationRegistry()
    _run(FakeKnowledge(), "search_materials", '{"query": "调度"}', registry)

    kinds = [item["kind"] for item in registry.citations]
    assert len(registry.citations) == SEARCH_LIMIT + WIKI_SEARCH_LIMIT
    assert kinds.count("material") == SEARCH_LIMIT
    materials = [item for item in registry.citations if item["kind"] == "material"]
    assert [item["chunk_id"] for item in materials] == [f"chunk_{i}" for i in range(1, SEARCH_LIMIT + 1)]


def test_the_sources_stay_out_of_the_tool_text():
    """出处只走引用通道，不进工具正文。摆到模型眼前，它就会把自己没读过的页码抄进回答，
    那是没有依据的引用。这一条也保证这次改动不会顺带改掉模型看到的东西。"""
    page = _run(FakeKnowledge(), "wiki_read", '{"concept_id": "cpt_1"}')
    hits = _run(FakeKnowledge(), "search_materials", '{"query": "调度"}')

    for text in (page.text, hits.text):
        assert "第 9 页原文" not in text and "chunk_src_9" not in text


def test_the_port_declares_the_wiki_source_capability():
    """出处也是服务端给的：模型指定不了要拿哪一页的出处，更改不了它指向哪几页。"""
    assert hasattr(KnowledgeSearchPort, "wiki_sources")
    assert "scope" in KnowledgeSearchPort.wiki_sources.__annotations__
    assert "course_id" not in KnowledgeSearchPort.wiki_sources.__annotations__


def test_the_port_declares_the_wiki_search_capability():
    """名额是服务端给的：模型既指定不了课程，也指定不了两边各取几条。"""
    assert hasattr(KnowledgeSearchPort, "search_wiki")
    assert "scope" in KnowledgeSearchPort.search_wiki.__annotations__
    assert "course_id" not in KnowledgeSearchPort.search_wiki.__annotations__


# ---------------------------------------------------------------- 检索库

@pytest.fixture
def indexed(tmp_path):
    """真的 KnowledgeService：知识页要真的进了检索库才查得到。"""
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="demo", text_base_url="", text_api_key="", text_model="", enable_remote_llm=False,
        chunk_size=120, chunk_overlap=20, top_k_results=6,
    )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    repository = KnowledgeRepository(store)
    course = CourseService(CourseRepository(store)).create_course(name="操作系统")
    material = repository.create_material(
        course_id=course.id, filename="os.md", storage_path=data_dir / "os.md",
        mime_type="text/markdown", byte_size=10,
    )
    repository.replace_chunks(
        material_id=material.id, course_id=course.id,
        chunks=[(1, "先来先服务调度会产生护航效应。"), (2, "时间片轮转按固定时长切换。")],
    )
    wiki_store = WikiStore(settings.data_dir)
    service = KnowledgeService(
        repository=repository, settings=settings,
        wiki_is_enabled=lambda _course_id: True, wiki_store=wiki_store,
    )
    return service, repository, wiki_store, course.id, material.id


def _write_index_page(wiki_store: WikiStore, course_id: str, material_id: str) -> None:
    wiki_store.write(
        course_id=course_id, concept_id="index", concept_name="课程总览",
        body="这门课整体分成三大块：进程与调度、内存管理、文件系统。建议按这个顺序读。",
        source_hash="h1", source_refs=[], updated_at="2026-08-01T00:00:00Z",
        material_id=material_id, level=0, order=-1,
    )


def test_built_wiki_pages_enter_the_retrieval_store(indexed):
    """构建完成后每页正文都要有一行可检索记录，否则种子检索永远看不见它。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    rows = repository.list_wiki_rows(course_id=course_id)
    assert [row["concept_id"] for row in rows] == ["index"]
    assert "三大块" in rows[0]["content"]


def test_wiki_search_finds_the_page_that_only_the_wiki_can_answer(indexed):
    """「这门课整体分成哪几部分」在教材原文里没有一段能答，知识页里有。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    scope = ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1")
    hits = service.search_wiki(scope=scope, query="这门课整体分成哪几部分", limit=WIKI_SEARCH_LIMIT)

    assert hits and hits[0].citation.kind == "wiki"
    assert hits[0].citation.concept_id == "index" and hits[0].citation.concept_name == "课程总览"
    assert hits[0].citation.page is None


def test_material_lexical_fallback_never_returns_wiki_rows(indexed):
    """两路必须分开取。这里走的是 LIKE 兜底：短词进不了 trigram 索引，
    而知识页本来就不在 FTS 里，只有兜底这一路会真的把它们扫出来。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    # 「调度」两个字进不了 trigram（不足三字），必然落到 LIKE 兜底
    hits = repository.search(course_id=course_id, query="调度", limit=6)

    assert hits, "教材那一路本来就该有命中"
    assert all(hit.citation.kind == "material" for hit in hits)


def test_material_dense_pool_never_loads_wiki_vectors(indexed):
    """向量那一路同理：两边的向量各装各的池子，默认只给教材原文。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    repository.replace_wiki_chunks(
        course_id=course_id,
        pages=[{"concept_id": "index", "concept_name": "课程总览",
                "material_id": material_id, "content": "整体分成三大块"}],
        embeddings=[b"\x00" * 8],
    )

    assert repository.load_course_embeddings(course_id=course_id) == []
    assert len(repository.load_course_embeddings(course_id=course_id, source_kind="wiki")) == 1


def test_material_search_never_returns_wiki_rows(indexed):
    """常规路径（trigram FTS）上也要成立。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    hits = service.search_course(course_id=course_id, query="进程与调度 护航效应", limit=6)

    assert hits, "教材那一路本来就该有命中"
    assert all(hit.citation.kind == "material" for hit in hits)


def test_wiki_search_is_empty_while_the_course_has_wiki_off(indexed):
    """课程没开知识页就当作没有页，和 wiki_index 同一个口径。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)
    service._wiki_is_enabled = lambda _course_id: False

    scope = ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1")
    assert service.search_wiki(scope=scope, query="这门课整体分成哪几部分") == []


def _write_leaf_page(wiki_store: WikiStore, course_id: str, material_id: str, refs: list[str],
                     *, concept_id: str = "cpt_fifo", parent_id: str | None = None) -> None:
    wiki_store.write(
        course_id=course_id, concept_id=concept_id, concept_name="先来先服务",
        body="长作业排在前面会拖住后面的短作业。[p.1]", source_hash="h2", source_refs=refs,
        updated_at="2026-08-01T00:00:00Z", material_id=material_id, parent_id=parent_id, level=1, order=1,
    )


def _refs_of(service: KnowledgeService, repository: KnowledgeRepository, material_id: str) -> list[str]:
    """按构建时的写法拼出处行：文档名 + 页码 + 分片 id。"""
    return [f"os.md p.{chunk['page']} #{chunk['id']}"
            for chunk in repository.list_material_chunks(material_id=material_id)]


def test_a_wiki_page_reports_the_textbook_pages_behind_it(indexed):
    """落盘的 frontmatter 里本来就记着出处，把它读出来标准化成可点开的页。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id, _refs_of(service, repository, material_id))

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert [(item.document, item.page) for item in sources.anchors] == [("os.md", 1), ("os.md", 2)]
    assert sources.pages == 2
    assert "护航效应" in sources.anchors[0].snippet


def test_an_overview_page_collects_the_pages_of_its_children(indexed):
    """总览页自己不读原文，记的是子页。出处得顺着子页收上来，
    否则最常被引的那几页（首页、章节页）反而一页都追不到。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id, _refs_of(service, repository, material_id),
                     parent_id="cpt_chapter")
    wiki_store.write(course_id=course_id, concept_id="cpt_chapter", concept_name="调度",
                     body="这一章从 FIFO 讲到 STCF。", source_hash="h3",
                     source_refs=["子页 先来先服务 <cpt_fifo>"], updated_at="2026-08-01T00:00:00Z",
                     material_id=material_id, level=0, order=0)
    _write_index_page(wiki_store, course_id, material_id)
    scope = ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1")

    chapter = service.wiki_sources(scope=scope, concept_id="cpt_chapter")
    index = service.wiki_sources(scope=scope, concept_id="index")

    assert [(item.document, item.page) for item in chapter.anchors] == [("os.md", 1), ("os.md", 2)]
    assert [(item.document, item.page) for item in index.anchors] == [("os.md", 1), ("os.md", 2)]


def test_a_page_whose_chunks_were_reindexed_still_names_them(indexed):
    """重建索引会换分片 id。取不到原文时仍要给出文档与页码——出处不能整条消失。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id, ["os.md p.7 #chunk_gone"])

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert [(item.document, item.page, item.snippet) for item in sources.anchors] == [("os.md", 7, "")]


def test_each_document_lists_at_most_a_handful_of_pages(indexed):
    """一门大课的首页依据整本书，全摆出来会淹掉抽屉。截断后区间两端仍要准。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id,
                     [f"os.md p.{page} #chunk_{page}" for page in range(1, 31)])

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    pages = [item.page for item in sources.anchors]
    assert len(pages) == wiki.WIKI_SOURCE_MAX_PAGES and sources.pages == 30
    assert pages[0] == 1 and pages[-1] == 30


def test_a_course_with_wiki_off_reports_no_sources(indexed):
    """和 wiki_index、search_wiki 同一个口径：课程没开知识页就当作没有页。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id, _refs_of(service, repository, material_id))
    service._wiki_is_enabled = lambda _course_id: False

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert sources.anchors == () and sources.pages == 0


def test_deleting_the_material_takes_its_wiki_rows_with_it(indexed):
    """检索行挂在 chunks 上就是为了这个：删教材、删课程那两条既有的清理链路照样收走它们，
    不必再各自加一句。留下来就是孤儿——教材没了，转述还在被检索到。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    assert service.reindex_wiki_pages(course_id=course_id, material_id=material_id) == 1

    repository.delete_material(material_id)

    assert repository.list_wiki_rows(course_id=course_id) == []


# ---------------------------------------------------------------- 上屏

def _course_id(client: TestClient, name: str) -> str:
    return next(item["id"] for item in client.get("/api/v2/courses").json() if item["name"] == name)


def test_the_turn_streams_material_and_wiki_citations(tmp_path):
    """SSE 上两类 kind 都要出现，前端才分得开转述与原文。"""
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        session_id = _indexed_course_session(client, name="操作系统", text="FIFO 调度会产生护航效应。")
        course_id = _course_id(client, "操作系统")
        client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
        space = workspace(client)
        WikiStore(space.settings.data_dir).write(
            course_id=course_id, concept_id="index", concept_name="课程总览",
            body="这门课整体分成三大块：进程与调度、内存管理、文件系统。",
            source_hash="h1", source_refs=[], updated_at="2026-08-01T00:00:00Z", level=0, order=-1,
        )
        material_id = client.get(f"/api/v2/courses/{course_id}/materials").json()[0]["id"]
        space.knowledge.reindex_wiki_pages(course_id=course_id, material_id=material_id)
        space.turns._responder = ScriptedChat([
            [ChatDelta("分成三大块 [1][2]。"),
             ChatFinal("分成三大块 [1][2]。", "stop", "example", "example-model", "provider")],
        ])

        events = _events(client.post(
            f"/api/v2/sessions/{session_id}/turns",
            json={"client_request_id": "cite-mix", "message": "这门课整体分成哪几部分？护航效应是什么"},
        ).text)

    kinds = {data.get("kind") for name, data in events if name == "citation"}
    assert kinds == {"material", "wiki"}


def test_the_turn_streams_the_pages_behind_a_wiki_citation(tmp_path):
    """整条链路走通才算：出处要跟着 SSE 上屏，界面才有东西可展开。"""
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        session_id = _indexed_course_session(client, name="操作系统", text="FIFO 调度会产生护航效应。")
        course_id = _course_id(client, "操作系统")
        client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
        space = workspace(client)
        material_id = client.get(f"/api/v2/courses/{course_id}/materials").json()[0]["id"]
        # markdown 上传没有页码，这里换成带页码的分片，出处那一页才有东西可标。
        space.knowledge._repository.replace_chunks(
            material_id=material_id, course_id=course_id, chunks=[(4, "FIFO 调度会产生护航效应。")])
        chunk = space.knowledge._repository.list_material_chunks(material_id=material_id)[0]
        WikiStore(space.settings.data_dir).write(
            course_id=course_id, concept_id="index", concept_name="课程总览",
            body="这门课整体分成三大块：进程与调度、内存管理、文件系统。",
            source_hash="h1", source_refs=[f"notes.md p.4 #{chunk['id']}"],
            updated_at="2026-08-01T00:00:00Z", material_id=material_id, level=0, order=-1,
        )
        space.knowledge.reindex_wiki_pages(course_id=course_id, material_id=material_id)
        space.turns._responder = ScriptedChat([
            [ChatDelta("分成三大块 [1]。"),
             ChatFinal("分成三大块 [1]。", "stop", "example", "example-model", "provider")],
        ])

        events = _events(client.post(
            f"/api/v2/sessions/{session_id}/turns",
            json={"client_request_id": "cite-src", "message": "这门课整体分成哪几部分？"},
        ).text)

    wiki_citation = next(data for name, data in events if name == "citation" and data.get("kind") == "wiki")
    assert [(item["document"], item["page"]) for item in wiki_citation["sources"]] == [("notes.md", 4)]
    assert wiki_citation["source_pages"] == 1


def test_the_context_reports_wiki_body_apart_from_textbook_evidence(tmp_path):
    """转述与原文在上下文构成里也要分开报，否则「教材证据 N token」里混着二手内容。"""
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        session_id = _indexed_course_session(client, name="操作系统", text="FIFO 调度会产生护航效应。")
        course_id = _course_id(client, "操作系统")
        client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
        space = workspace(client)
        WikiStore(space.settings.data_dir).write(
            course_id=course_id, concept_id="index", concept_name="课程总览",
            body="这门课整体分成三大块：进程与调度、内存管理、文件系统。",
            source_hash="h1", source_refs=[], updated_at="2026-08-01T00:00:00Z", level=0, order=-1,
        )
        material_id = client.get(f"/api/v2/courses/{course_id}/materials").json()[0]["id"]
        space.knowledge.reindex_wiki_pages(course_id=course_id, material_id=material_id)
        space.turns._responder = ScriptedChat([
            [ChatDelta("好。"), ChatFinal("好。", "stop", "example", "example-model", "provider")],
        ])

        events = _events(client.post(
            f"/api/v2/sessions/{session_id}/turns",
            json={"client_request_id": "cite-ctx", "message": "这门课整体分成哪几部分？"},
        ).text)

    usage = next(data for name, data in events if name == "context_usage")
    segments = {item["label_key"]: item["tokens"] for item in usage["segments"]}
    assert segments["context.segment.wiki_evidence"] > 0
    assert sum(segments.values()) == usage["total_tokens"]
