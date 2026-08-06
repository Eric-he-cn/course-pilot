"""知识页成为第三类可引用来源。

在这之前知识页只能读、不能引：模型越用它，回答里就越多结论没有出处可点。
这一批守三件事——知识页进检索库并有自己的固定名额、教材席位一条不少、
引用条目分得清哪条是转述哪条是原文。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.knowledge import (
    HANDWRITTEN_LABEL, Citation, KnowledgeHit, KnowledgeSearchPort, ResolvedKnowledgeScope,
    WikiDocument, WikiSource, WikiSources,
)
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest
from core.settings import Settings
from core.store import SQLiteStore
from modules.agent.tools import (
    MAIN,
    MAIN_PROFILE,
    SEARCH_LIMIT,
    WIKI_HIT_MAX_CHARS,
    WIKI_NOTE_MAX_CHARS,
    WIKI_PAGE_MAX_CHARS,
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


def test_every_place_that_hands_over_a_wiki_page_asks_for_textbook_backing():
    """实测模型拿到转述稿就不回教材取证，关键结论因此没有页码可点。
    系统提示、读页、检索命中、工具描述四处都要提这一句——系统提示每轮常驻、位置最强，
    它留在旧口径上会把另外三处压回去。"""
    from modules.agent.context import WIKI_ATTRIBUTION_NOTE, _wiki_block
    from modules.agent.tools import TOOL_SPECS

    system = _wiki_block([("cpt_1", "护航效应")], 1)
    description = next(spec.description for spec in TOOL_SPECS if spec.name == "wiki_read")
    page = _run(FakeKnowledge(), "wiki_read", '{"concept_id": "cpt_1"}').text
    hits = _run(FakeKnowledge(materials=0, wiki=1), "search_materials", '{"query": "整体结构"}').text

    for text in (system, description, page, hits):
        assert WIKI_ATTRIBUTION_NOTE in text
    # 要求是「知识页不能独自撑起结论」，不是「想要页码时自己去查」——后者模型不想要就不查。
    assert "唯一出处" in WIKI_ATTRIBUTION_NOTE
    assert "search_materials" in WIKI_ATTRIBUTION_NOTE, "要点名回哪个工具，否则模型不知道去哪查"
    assert "不必重复查" in WIKI_ATTRIBUTION_NOTE, "手头够用就别再搜，否则每句话都发一次检索"
    assert "要给出教材页码就用" not in system, "旧的弱措辞不能和新口径同时摆在系统提示里"


# ------------------------------------------------- 手写区在渲染时独立留位

# 真实页正文的长度量级：实测 85% 的页超过 WIKI_HIT_MAX_CHARS，短正文的 fixture 量不出截断。
_LONG_BODY = "循环神经网络在每个时间步复用同一套参数，把上一步的隐状态传给下一步。" * 25
_NOTE = "老师说这一节的 Wxh 下标顺序和课件是反的，考试按课件写。"


class LongPageKnowledge(FakeKnowledge):
    """一页真实长度的知识页，手写区在正文之后——被截掉的正是这一段。"""

    def __init__(self, *, note: str = _NOTE) -> None:
        super().__init__(materials=0, wiki=0)
        self._note = note
        content = f"循环神经网络\n\n{_LONG_BODY}"
        if note:
            content += f"\n\n{HANDWRITTEN_LABEL}\n{note}"
        self._wiki = [KnowledgeHit(
            citation=Citation(material_id="m1", document="", page=None, chunk_id="wiki:cpt_rnn",
                              snippet="循环神经网络的整理稿开头", score=0.9, kind="wiki",
                              concept_id="cpt_rnn", concept_name="循环神经网络"),
            content=content,
        )]

    def wiki_read(self, *, scope, concept_id):
        return WikiDocument(concept_id, "循环神经网络", _LONG_BODY, self._note)


def test_a_long_page_still_shows_the_users_note_in_the_search_hits():
    """整段一起截，用户的补充会被生成区挤没——而他写它就是为了纠偏。"""
    text = _run(LongPageKnowledge(), "search_materials", '{"query": "RNN 每步算什么"}').text

    assert len(_LONG_BODY) > WIKI_HIT_MAX_CHARS, "fixture 的正文要真的超上限，否则这条恒绿"
    assert HANDWRITTEN_LABEL in text and _NOTE in text
    assert "…" in text, "生成区仍然要被截断，手写区不是给正文买来的额度"


def test_the_generated_half_is_still_held_to_its_own_limit():
    """留位不等于放宽：正文那一段照旧只给 WIKI_HIT_MAX_CHARS。"""
    text = _run(LongPageKnowledge(), "search_materials", '{"query": "RNN"}').text

    body = text.split(HANDWRITTEN_LABEL)[0]
    assert len(body.split("循环神经网络\n", 1)[-1]) <= WIKI_HIT_MAX_CHARS + 8


def test_a_very_long_note_is_clipped_on_its_own():
    """手写区没有落盘上限（只挡 128 KiB），一整屏批注不能把这一轮的上下文吃光。"""
    text = _run(LongPageKnowledge(note="补" * 900), "search_materials", '{"query": "RNN"}').text

    note = text.split(HANDWRITTEN_LABEL, 1)[1]
    assert 0 < len(note.strip()) <= WIKI_NOTE_MAX_CHARS + 8 and note.strip().endswith("…")


def test_a_page_without_a_note_renders_exactly_as_before():
    """绝大多数页没有手写区，它们的渲染一个字节都不该变。"""
    text = _run(LongPageKnowledge(note=""), "search_materials", '{"query": "RNN"}').text

    assert HANDWRITTEN_LABEL not in text
    assert text.count("…") == 1, "只有生成区被截，没有多出第二个截断点"


def test_a_label_in_the_generated_half_does_not_swallow_the_real_note():
    """端到端的那条线：页里两处标注时，模型看到的手写区仍是完整的那一段。
    组装时摘掉生成区里那处，读的一端第一处标注就一定是真起点。"""
    document = WikiDocument("cpt_rnn", "循环神经网络", f"{_LONG_BODY}{HANDWRITTEN_LABEL}模型抄的", _NOTE)
    knowledge = LongPageKnowledge()
    knowledge._wiki[0] = KnowledgeHit(
        citation=knowledge._wiki[0].citation, content=wiki.retrieval_content(document))

    text = _run(knowledge, "search_materials", '{"query": "RNN"}').text

    assert text.count(HANDWRITTEN_LABEL) == 1
    assert _NOTE in text, "真手写区不能被生成区里那处标注挤掉"


def test_wiki_read_keeps_the_note_when_the_page_fills_the_limit():
    """读页那一路同型：正文顶满 6000 时，整段截会把用户那一段一起丢掉。"""
    knowledge = LongPageKnowledge()
    knowledge.wiki_read = lambda *, scope, concept_id: WikiDocument(  # noqa: ARG005
        concept_id, "循环神经网络", "长" * (WIKI_PAGE_MAX_CHARS + 500), _NOTE)

    text = _run(knowledge, "wiki_read", '{"concept_id": "cpt_rnn"}').text

    assert HANDWRITTEN_LABEL in text and _NOTE in text
    assert "没有给出" in text, "正文被截了要如实说剩多少"


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


def test_a_page_without_a_note_keeps_the_plain_two_block_row(indexed):
    """没写手写区的页，检索行还是「概念名 + 正文」两段，不带身份标注那一段。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    content = repository.list_wiki_rows(course_id=course_id)[0]["content"]
    assert wiki.HANDWRITTEN_LABEL not in content
    assert content == "课程总览\n\n这门课整体分成三大块：进程与调度、内存管理、文件系统。建议按这个顺序读。"


def test_a_handwritten_note_enters_the_retrieval_row_with_its_identity(indexed):
    """用户的纠偏要进检索，但得带着身份：命中之后模型只看得到这一行，不标注就当成教材转述。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    wiki_store.write_handwritten(course_id=course_id, concept_id="index",
                                 text="老师说本学期不考文件系统。")
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    content = repository.list_wiki_rows(course_id=course_id)[0]["content"]
    assert "老师说本学期不考文件系统。" in content
    assert wiki.HANDWRITTEN_LABEL in content
    assert content.index(wiki.HANDWRITTEN_LABEL) < content.index("老师说本学期不考文件系统。")
    assert content.startswith("课程总览\n\n"), "概念名仍在第一段，引用摘要按这个位置切"


def test_the_note_alone_makes_the_page_findable(indexed):
    """判在用户能感知的性质上：只有手写区里才有的说法，问它要能命中这一页。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    wiki_store.write_handwritten(course_id=course_id, concept_id="index",
                                 text="老师说本学期不考文件系统，期末只考调度那一块。")
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    scope = ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1")
    hits = service.search_wiki(scope=scope, query="本学期不考什么", limit=WIKI_SEARCH_LIMIT)

    assert [hit.citation.concept_id for hit in hits] == ["index"]
    assert "本学期不考文件系统" in hits[0].content, "模型看到的正文里要有这段批注"


def test_the_note_is_embedded_together_with_the_page(indexed):
    """向量化用的是同一份文本：只进词面不进向量，语义相近但用词不同的提问仍然找不到批注。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    embedded: list[str] = []

    class RecordingEmbedder:
        name = "recording"

        def status(self) -> dict[str, object]:
            return {"model": self.name, "loaded": True, "error": None}

        def embed_documents(self, texts: list[str]) -> list[bytes]:
            embedded.extend(texts)
            return [b"\x00" * 8 for _ in texts]

        def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
            return []

    service._embedder = RecordingEmbedder()
    _write_index_page(wiki_store, course_id, material_id)
    wiki_store.write_handwritten(course_id=course_id, concept_id="index", text="老师说本学期不考文件系统。")
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    assert len(embedded) == 1 and "老师说本学期不考文件系统。" in embedded[0]
    assert wiki.HANDWRITTEN_LABEL in embedded[0]


def test_reindexing_without_an_embedder_still_writes_the_rows(indexed):
    """没配嵌入模型的装机也要能刷新检索行：这一路只是没有向量，词面检索照常。"""
    service, repository, wiki_store, course_id, material_id = indexed
    assert service._embedder is None
    _write_index_page(wiki_store, course_id, material_id)
    wiki_store.write_handwritten(course_id=course_id, concept_id="index", text="老师说本学期不考文件系统。")

    assert service.reindex_wiki_pages(course_id=course_id, material_id=material_id) == 1

    assert repository.load_course_embeddings(course_id=course_id, source_kind="wiki") == []
    assert "老师说本学期不考文件系统。" in repository.list_wiki_rows(course_id=course_id)[0]["content"]


def test_a_page_with_no_recorded_material_hangs_on_the_one_that_triggered_the_build(indexed):
    """课程首页不属于任何一份教材（frontmatter 的归属是空的）。它得挂在某份活着的教材上，
    否则这门课的总览永远进不了检索。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    wiki_store.write(course_id=course_id, concept_id="index", concept_name="课程总览",
                     body="这门课整体分成三大块。", source_hash="h1", source_refs=[],
                     updated_at="2026-08-01T00:00:00Z", level=0, order=-1)  # material_id 留空

    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    with repository._store.read() as conn:
        owners = {row["concept_id"]: row["material_id"] for row in conn.execute(
            "SELECT concept_id, material_id FROM chunks WHERE course_id = ? AND source_kind = 'wiki'",
            (course_id,))}
    assert owners == {"index": material_id}


def test_a_page_whose_material_is_gone_stays_out_of_the_retrieval_store(indexed):
    """删教材不删页文件（那是为了保用户手写）。这样的页不能再当证据进检索——
    它的教材已经没了，引用点开是一条死链，重建别的教材时也不该把它挂到别人名下。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    _write_leaf_page(wiki_store, course_id, "material_已删掉的", refs=[], concept_id="cpt_orphan")

    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)

    assert [row["concept_id"] for row in repository.list_wiki_rows(course_id=course_id)] == ["index"]


def test_one_unreadable_page_does_not_cost_the_course_its_other_rows(indexed):
    """一页坏文件（按 GBK 存过的、读到一半被搬走的）不该让整门课的检索行刷不成——
    那样界面上什么都不报，模型却再也检索不到这门课的知识页。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_index_page(wiki_store, course_id, material_id)
    _write_leaf_page(wiki_store, course_id, material_id, refs=[])
    broken = wiki_store._locate(course_id=course_id, concept_id="index")
    # frontmatter 的 concept_id 是 ASCII，这一页照样列得出来，读正文时才炸
    broken.write_bytes(broken.read_text(encoding="utf-8").encode("gbk"))

    assert service.reindex_wiki_pages(course_id=course_id, material_id=material_id) == 1

    assert [row["concept_id"] for row in repository.list_wiki_rows(course_id=course_id)] == ["cpt_fifo"]


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
    """重建索引会换分片 id。教材里也没有那一页时仍要给出文档与页码——出处不能整条消失。"""
    service, _repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id, ["os.md p.7 #chunk_gone"])

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert [(item.document, item.page, item.snippet) for item in sources.anchors] == [("os.md", 7, "")]


def test_reindexing_the_material_does_not_break_wiki_source_anchors(indexed):
    """重建索引把分片 id 整表换掉，但那几页还在。出处要按（教材, 页码）解析回当前分片，
    引用抽屉里的原文不能因为一次重建索引就整批变空。"""
    service, repository, wiki_store, course_id, material_id = indexed
    _write_leaf_page(wiki_store, course_id, material_id, _refs_of(service, repository, material_id))
    # 同一份教材再索引一遍：内容与页码都没变，分片 id 全部更换
    repository.replace_chunks(
        material_id=material_id, course_id=course_id,
        chunks=[(1, "先来先服务调度会产生护航效应。"), (2, "时间片轮转按固定时长切换。")],
    )
    alive = {chunk["id"] for chunk in repository.list_material_chunks(material_id=material_id)}

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert [(item.document, item.page) for item in sources.anchors] == [("os.md", 1), ("os.md", 2)]
    # 逐页断言内容：解析要真按页码落位，全指到文件开头这里就红
    assert "护航效应" in sources.anchors[0].snippet
    assert "时间片" in sources.anchors[1].snippet
    assert all(item.chunk_id in alive for item in sources.anchors), "锚点要指向现存分片，点开抽屉才有内容"


def test_a_live_recorded_chunk_id_is_kept_while_dead_ones_relocate(indexed):
    """混合态：一条 ref 的 id 还活着、另一条死了。活的照记录用（它是构建时真读过的
    那个分片），死的按位置解析——两条路互不影响。"""
    service, repository, wiki_store, course_id, material_id = indexed
    live = repository.list_material_chunks(material_id=material_id)[0]
    _write_leaf_page(wiki_store, course_id, material_id,
                     [f"os.md p.1 #{live['id']}", "os.md p.2 #chunk_dead"])

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert sources.anchors[0].chunk_id == live["id"]
    assert "时间片" in sources.anchors[1].snippet and sources.anchors[1].chunk_id != "chunk_dead"


def test_same_named_materials_do_not_cross_resolve(indexed):
    """同课程两份同名教材：出处按页的归属教材解析，不能指到另一份的内容。"""
    service, repository, wiki_store, course_id, material_id = indexed
    other = repository.create_material(
        course_id=course_id, filename="os.md", storage_path=Path("/nonexistent/os2.md"),
        mime_type="text/markdown", byte_size=10,
    )
    repository.replace_chunks(material_id=other.id, course_id=course_id,
                              chunks=[(1, "另一份同名文件，内容毫无关系。")])
    # 记出处的那份刚重建过索引：它的行比同名教材的行新，按文件名找会先撞上旧的那份
    repository.replace_chunks(
        material_id=material_id, course_id=course_id,
        chunks=[(1, "先来先服务调度会产生护航效应。"), (2, "时间片轮转按固定时长切换。")],
    )
    _write_leaf_page(wiki_store, course_id, material_id, ["os.md p.1 #chunk_dead"])

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert "护航效应" in sources.anchors[0].snippet, "要解析回记这条出处的那份教材"


def test_a_pageless_ref_is_not_faked_from_the_file_head(indexed):
    """无页码教材的出处没有位置可解析：记录的 id 死了就保持空，不能拿文件开头顶替，
    也不能撞上检索库里同样无页码的知识页行。"""
    service, repository, wiki_store, course_id, material_id = indexed
    # txt/md 教材的真实形状：分片没有页码。开头那段与出处指的那段不是同一段。
    repository.replace_chunks(
        material_id=material_id, course_id=course_id,
        chunks=[(None, "开头：课程简介与致谢。"), (None, "中段：护航效应的推导。")],
    )
    _write_index_page(wiki_store, course_id, material_id)
    service.reindex_wiki_pages(course_id=course_id, material_id=material_id)
    _write_leaf_page(wiki_store, course_id, material_id, ["os.md #chunk_dead"])

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_fifo")

    assert [(item.document, item.page, item.snippet) for item in sources.anchors] == [("os.md", None, "")]


def test_diverse_seats_is_the_same_rule_on_every_retrieval_path():
    """三条检索路（词法、重排、RRF 融合）出口都过同一个函数。fixture 只走得到词法那条，
    这里把函数本身当纯函数钉住——多 owner 先各取一条、单 owner 不受影响、原序不乱。"""
    from modules.knowledge.service import _diverse_by_material

    def hit(name: str, owner: str) -> KnowledgeHit:
        return _wiki_hit(name, name)._replace_owner(owner) if False else KnowledgeHit(
            citation=Citation(material_id=owner, document="", page=None, chunk_id=f"wiki:{name}",
                              snippet=name, score=0.9, kind="wiki", concept_id=name, concept_name=name),
            content=name)

    a1, a2, b1 = hit("a1", "mat_a"), hit("a2", "mat_a"), hit("b1", "mat_b")
    assert _diverse_by_material([a1, a2, b1], 2) == [a1, b1], "两席应先给两本教材各一条"
    assert _diverse_by_material([a1, a2], 2) == [a1, a2], "单教材照原序补满"
    assert _diverse_by_material([a1, b1, a2], 3) == [a1, b1, a2], "席位够时一条不丢"
    assert _diverse_by_material([], 2) == []


def test_source_anchors_cap_counts_per_material_not_per_filename(indexed):
    """两份同名教材各自吃自己的页数上限：并成一组会静默丢一份的页，也会让归属指错。"""
    service, repository, wiki_store, course_id, material_id = indexed
    other = repository.create_material(
        course_id=course_id, filename="os.md", storage_path=Path("/nonexistent/os2.md"),
        mime_type="text/markdown", byte_size=10,
    )
    for owner in (material_id, other.id):
        repository.replace_chunks(material_id=owner, course_id=course_id,
                                  chunks=[(page, f"{owner} 第 {page} 页") for page in range(1, 13)])
    refs = [f"os.md p.{chunk['page']} #{chunk['id']}"
            for owner in (material_id, other.id)
            for chunk in repository.list_material_chunks(material_id=owner)]
    # 两份教材的出处写进同一页：总览页顺着子页收出处时正是这个形状
    wiki_store.write(course_id=course_id, concept_id="cpt_both", concept_name="双书总览",
                     body="两本书的对照。", source_hash="h9", source_refs=refs[:12],
                     updated_at="2026-08-01T00:00:00Z", material_id=material_id, level=0, order=0)
    wiki_store.write(course_id=course_id, concept_id="cpt_child", concept_name="乙书细节",
                     body="乙书的细节。", source_hash="h10", source_refs=refs[12:],
                     updated_at="2026-08-01T00:00:00Z", material_id=other.id,
                     parent_id="cpt_both", level=1, order=1)

    sources = service.wiki_sources(
        scope=ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1"),
        concept_id="cpt_both")

    owners = {}
    for anchor in sources.anchors:
        owners[anchor.material_id] = owners.get(anchor.material_id, 0) + 1
    assert owners == {material_id: wiki.WIKI_SOURCE_MAX_PAGES, other.id: wiki.WIKI_SOURCE_MAX_PAGES}
    assert sources.pages == 24


def test_wiki_seats_prefer_distinct_materials(indexed):
    """两本书都讲这个主题时，两个席位不能被一本书占满——先每本各取一条，有剩再补。"""
    service, repository, wiki_store, course_id, material_id = indexed
    other = repository.create_material(
        course_id=course_id, filename="notes.md", storage_path=Path("/nonexistent/notes.md"),
        mime_type="text/markdown", byte_size=10,
    )
    repository.replace_wiki_chunks(
        course_id=course_id,
        pages=[{"concept_id": "a1", "concept_name": "调度总览", "material_id": material_id,
                "content": "调度总览\n\n先来先服务与时间片轮转的调度算法比较。"},
               {"concept_id": "a2", "concept_name": "调度细节", "material_id": material_id,
                "content": "调度细节\n\n先来先服务调度算法的护航效应细节。"},
               {"concept_id": "b1", "concept_name": "调度笔记", "material_id": other.id,
                "content": "调度笔记\n\n另一本讲调度算法的笔记：先来先服务。"}],
    )

    scope = ResolvedKnowledgeScope(turn_id="t1", course_id=course_id, resolver_version="v1")
    hits = service.search_wiki(scope=scope, query="先来先服务调度算法", limit=2)

    owners = {hit.citation.material_id for hit in hits}
    assert len(hits) == 2 and owners == {material_id, other.id}, "两席应来自两本教材"


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


def test_the_sources_route_serves_anchors_and_honors_the_limit(tmp_path):
    """正文 [p.N] 接线走这条路由。两侧失败都是静默的（前端空 catch、未知页返回空表），
    路由本身必须有判据钉住，路径拼错一个字符不能只有界面上「全都点不开」这一种信号。"""
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        _indexed_course_session(client, name="操作系统", text="FIFO 调度会产生护航效应。")
        course_id = _course_id(client, "操作系统")
        client.patch(f"/api/v2/courses/{course_id}", json={"wiki_enabled": True})
        space = workspace(client)
        material_id = client.get(f"/api/v2/courses/{course_id}/materials").json()[0]["id"]
        space.knowledge._repository.replace_chunks(
            material_id=material_id, course_id=course_id,
            chunks=[(page, f"第 {page} 页的正文。") for page in range(1, 13)])
        chunks = space.knowledge._repository.list_material_chunks(material_id=material_id)
        WikiStore(space.settings.data_dir).write(
            course_id=course_id, concept_id="cpt_long", concept_name="长页",
            body="跨了十二页的长节。", source_hash="h1",
            source_refs=[f"notes.md p.{chunk['page']} #{chunk['id']}" for chunk in chunks],
            updated_at="2026-08-01T00:00:00Z", material_id=material_id, level=0, order=0)

        space.knowledge._repository.replace_chunks(
            material_id=material_id, course_id=course_id,
            chunks=[(page, f"第 {page} 页的正文。") for page in range(1, 251)])
        huge = [f"notes.md p.{chunk['page']} #{chunk['id']}"
                for chunk in space.knowledge._repository.list_material_chunks(material_id=material_id)]
        WikiStore(space.settings.data_dir).write(
            course_id=course_id, concept_id="cpt_huge", concept_name="巨页",
            body="跨了二百五十页。", source_hash="h2", source_refs=huge,
            updated_at="2026-08-01T00:00:00Z", material_id=material_id, level=0, order=1)

        capped = client.get(f"/api/v2/courses/{course_id}/wiki/cpt_long/sources").json()
        full = client.get(f"/api/v2/courses/{course_id}/wiki/cpt_long/sources?limit=200").json()
        missing = client.get(f"/api/v2/courses/{course_id}/wiki/cpt_nowhere/sources").json()
        clamped = client.get(f"/api/v2/courses/{course_id}/wiki/cpt_huge/sources?limit=1000").json()

    assert len(capped["anchors"]) == wiki.WIKI_SOURCE_MAX_PAGES and capped["pages"] == 12
    assert sorted(capped["anchors"][0]) == ["chunk_id", "document", "material_id", "page", "snippet"]
    assert len(full["anchors"]) == 12 and all(item["snippet"] for item in full["anchors"])
    assert missing == {"anchors": [], "pages": 0}
    assert len(clamped["anchors"]) == 200 and clamped["pages"] == 250, "limit 超过 200 要按 200 截"


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
