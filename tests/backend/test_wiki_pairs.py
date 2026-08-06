"""知识页配对：哪几个来源在讲同一件事。

多本教材讲同一节时，纯 RAG 只会各召回各的，用户看不出这几页是一件事。配对把这层关系摆到页面上。
连边的判据全在纯函数里，用手造的相似度矩阵逐条钉住；集成那几条守「向量 → 边 → 接口」真的通着。
"""
from __future__ import annotations

import time
from array import array

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from adapters.embedding import BgeEmbedder, cosine_matrix
from app.main import create_app
from core.settings import Settings
from core.store import SQLiteStore
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge import wiki
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.wiki import WikiStore


def _vector(*values: float) -> bytes:
    """按库里的存法造一个向量：float32 字节串。"""
    return array("f", values).tobytes()


class VectorMath:
    """只会做向量运算的嵌入端口。配对读的是库里存好的向量，这一步本来就不该加载模型。"""

    name = "test-vectors"

    def status(self) -> dict[str, object]:
        return {"model": self.name, "loaded": True, "error": None}

    def embed_documents(self, texts: list[str]) -> list[bytes] | None:
        return None

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
        return []

    def pairwise(self, vectors: list[bytes]) -> list[list[float]] | None:
        return cosine_matrix(vectors)


class OldPort:
    """只有查询侧能力的旧适配器，没有 doc-doc 那一步。"""

    name = "old-port"

    def status(self) -> dict[str, object]:
        return {"model": self.name, "loaded": True, "error": None}

    def embed_documents(self, texts: list[str]) -> list[bytes] | None:
        return None

    def rank(self, *, query: str, vectors: list[bytes], top_k: int) -> list[tuple[int, float]]:
        return []


# ---- 纯函数：连边的每一条判据 ----

def _node(concept_id: str, *, material: str = "m1", parent: str = "") -> wiki.PairNode:
    """默认同属一本教材、都在顶层。要连边就把其中一边挪到 m2。"""
    return wiki.PairNode(concept_id=concept_id, material_id=material, parent_id=parent)


def _matrix(nodes: list[wiki.PairNode], scores: dict[tuple[str, str], float], base: float) -> list[list[float]]:
    """对称相似度矩阵：没点名的页对一律给 base。"""
    at = {node.concept_id: index for index, node in enumerate(nodes)}
    out = [[1.0 if row == column else base for column in range(len(nodes))] for row in range(len(nodes))]
    for (left, right), value in scores.items():
        out[at[left]][at[right]] = out[at[right]][at[left]] = value
    return out


def _pairs(nodes: list[wiki.PairNode], scores: dict[tuple[str, str], float],
           base: float = 0.05) -> set[frozenset[str]]:
    return {frozenset({str(edge["a"]), str(edge["b"])})
            for edge in wiki.pair_pages(nodes, _matrix(nodes, scores, base))}


def test_only_pages_that_pick_each_other_get_an_edge():
    """单向近邻不连：p0 把 p7 当最像的一页，而 p7 的六个近邻里没有它。
    同一份分数，让 p7 也把 p0 数进近邻（p6 掉出去），这条边才出现。"""
    nodes = [_node("p0", material="m2"), *(_node(f"p{i}", parent="chap") for i in range(1, 7)), _node("p7")]
    one_way = {("p0", "p7"): 0.8, **{("p7", f"p{i}"): 0.9 for i in range(1, 7)}}
    mutual = {**one_way, ("p7", "p6"): 0.05}

    assert frozenset({"p0", "p7"}) not in _pairs(nodes, one_way, base=0.3)
    assert frozenset({"p0", "p7"}) in _pairs(nodes, mutual, base=0.3)


def test_the_threshold_comes_from_how_similar_sibling_pages_are():
    """余弦的绝对值不跨库可比，门槛按同章兄弟自标定：这里三对兄弟是 0.4/0.6/0.8，中位数 0.6。
    x/y 的 0.7 过线，x/z 的 0.5 不过——z 另有一条 0.9 的边，落选不是因为它落了单。"""
    nodes = [*(_node(f"c{i}", parent="chap") for i in (1, 2, 3)),
             _node("x"), _node("y", material="m2"), _node("z", material="m2")]
    scores = {("c1", "c2"): 0.4, ("c1", "c3"): 0.6, ("c2", "c3"): 0.8,
              ("x", "y"): 0.7, ("x", "z"): 0.5, ("c1", "z"): 0.9}
    pairs = _pairs(nodes, scores)

    assert frozenset({"x", "y"}) in pairs and frozenset({"c1", "z"}) in pairs
    assert frozenset({"x", "z"}) not in pairs


def test_a_course_without_siblings_falls_back_to_the_median_of_every_pair():
    """页全在顶层的课一对兄弟都没有，门槛退回全部页对的中位数：十对的中位数是 0.4。
    同教材的那几对不连边，但照样参与标定——门槛量的是「这门课的页彼此有多像」。
    x/z 的 0.3 在门槛之下，两端各自另有边，所以保底也不会把它捡回来。"""
    nodes = [_node("x"), _node("v"), _node("y", material="m2"),
             _node("z", material="m2"), _node("w", material="m2")]
    scores = {("x", "y"): 0.9, ("v", "z"): 0.9, ("y", "z"): 0.7, ("y", "w"): 0.6, ("z", "w"): 0.5,
              ("x", "z"): 0.3, ("x", "w"): 0.1, ("x", "v"): 0.1, ("v", "y"): 0.1, ("v", "w"): 0.1}

    assert _pairs(nodes, scores) == {frozenset({"x", "y"}), frozenset({"v", "z"}), frozenset({"x", "w"})}


def test_the_threshold_counts_direct_siblings_only():
    """兄弟 = 同一个直系 parent。顶层页各属各的教材，凑成一组标出来的数代表不了「同章两页有多像」。
    保底会把门槛之下的边捡回来，端到端看不出这个差别，所以直接量标定本身。"""
    nodes = [_node("t1"), _node("t2"), _node("c1", parent="chap"), _node("c2", parent="chap")]
    matrix = _matrix(nodes, {("t1", "t2"): 0.9, ("c1", "c2"): 0.3}, 0.05)

    assert wiki._sibling_threshold(nodes, matrix) == pytest.approx(0.3)


def test_the_threshold_takes_the_median_not_the_average():
    """一对特别像的兄弟（0.99）会把平均数拉到 0.348，中位数仍是 0.2。"""
    nodes = [_node(f"c{index}", parent="chap") for index in range(1, 5)]
    matrix = _matrix(nodes, {("c1", "c2"): 0.2, ("c1", "c3"): 0.2, ("c1", "c4"): 0.2,
                             ("c2", "c3"): 0.2, ("c2", "c4"): 0.3, ("c3", "c4"): 0.99}, 0.05)

    assert wiki._sibling_threshold(nodes, matrix) == pytest.approx(0.2)


def test_two_pages_from_the_same_textbook_never_pair_up():
    """一门课只有一本书时本来就没有「几个来源」。分数一模一样，差别只在同不同一本教材。"""
    nodes = [_node("a1", parent="chap"), _node("a2", parent="chap"), _node("b1", material="m2")]
    scores = {("a1", "a2"): 0.9, ("a1", "b1"): 0.9}
    pairs = _pairs(nodes, scores)

    assert frozenset({"a1", "b1"}) in pairs and frozenset({"a1", "a2"}) not in pairs


def test_a_flat_single_textbook_course_shows_nothing():
    """真数据里的形状：无书签教材，页全在顶层、彼此都在讲调度（余弦 0.79~0.85）。
    没有兄弟对时门槛退回全对中位数，必然放行一半页对；挡住它们的是「只连跨教材」这一条。"""
    nodes = [_node(f"p{index}") for index in range(6)]
    scores = {("p0", "p1"): 0.85, ("p0", "p2"): 0.84, ("p1", "p2"): 0.83, ("p3", "p4"): 0.82}

    assert _pairs(nodes, scores, base=0.79) == set()


def test_a_page_left_with_nothing_keeps_its_best_neighbor_from_another_textbook():
    """「几本书都讲了这一节」正是这件功能要兑付的东西，不该被一个按同章标定出来的门槛挡掉。
    保底只看跨教材的候选：同教材的 y 更像（0.7），留下的仍是另一本书的 x（0.5）。"""
    nodes = [_node("s1", parent="chap"), _node("s2", parent="chap"), _node("x", material="m2"), _node("y")]
    scores = {("s1", "s2"): 0.9, ("s1", "x"): 0.5, ("s1", "y"): 0.7}

    edges = wiki.pair_pages(nodes, _matrix(nodes, scores, 0.05))
    rescued = next(edge for edge in edges if {edge["a"], edge["b"]} == {"s1", "x"})

    assert rescued["score"] == 0.5
    assert frozenset({"s1", "y"}) not in _pairs(nodes, scores)


def test_the_floor_does_not_reach_inside_one_textbook():
    """同一份分数，x 挪回同一本教材就一条边都不连：保底也只在教材之间兜。"""
    nodes = [_node("s1", parent="chap"), _node("s2", parent="chap"), _node("x"), _node("y")]
    scores = {("s1", "s2"): 0.9, ("s1", "x"): 0.5, ("s1", "y"): 0.2}

    assert _pairs(nodes, scores) == set()


def test_a_page_carries_at_most_three_edges():
    """这一行是读页时的旁注，不是目录。五个近邻只留最像的三个。"""
    nodes = [_node("hub"), *(_node(f"n{index}", material="m2") for index in range(1, 6))]
    scores = {("hub", f"n{index}"): score for index, score in zip(range(1, 6), (0.9, 0.8, 0.7, 0.6, 0.55))}

    edges = wiki.pair_pages(nodes, _matrix(nodes, scores, 0.05))
    around_hub = [edge for edge in edges if "hub" in {edge["a"], edge["b"]}]

    assert [edge["score"] for edge in around_hub] == [0.9, 0.8, 0.7]


def test_edges_come_back_strongest_first_and_say_which_two_pages_they_join():
    """输出是接口：两端加分数（四位小数够界面排序，多的只是浮点噪声），按分数从高到低。"""
    nodes = [_node("a"), _node("b", material="m2"), _node("c", material="m2")]
    edges = wiki.pair_pages(nodes, _matrix(nodes, {("a", "b"): 0.912345, ("a", "c"): 0.7}, 0.05))

    assert [(edge["a"], edge["b"], edge["score"]) for edge in edges] == [("a", "b", 0.9123), ("a", "c", 0.7)]


def test_pages_that_look_nothing_alike_never_get_an_edge():
    """门槛是相对量：一门课的页彼此都不像时它会一路降到 0，那时任何一对都「过线」。
    余弦不为正的两页在任何口径下都不是在讲同一件事，主路径与保底都不收。"""
    flat = [_node("a1", material="m1"), _node("a2", material="m1"),
            _node("b1", material="m2"), _node("b2", material="m2")]
    opposed = [_node("x"), _node("y", material="m2")]

    assert _pairs(flat, {}, base=0.0) == set()
    assert _pairs(opposed, {("x", "y"): -0.4}) == set()


def test_a_course_with_nothing_to_pair_has_no_edges():
    """空课、单页、以及矩阵和页数对不上（调用方接错了）都返回空表，不抛。"""
    assert wiki.pair_pages([], []) == []
    assert wiki.pair_pages([_node("only")], [[1.0]]) == []
    assert wiki.pair_pages([_node("a"), _node("b")], [[1.0]]) == []
    assert wiki.pair_pages([_node("a"), _node("b")], [[1.0, 0.5], [0.5]]) == []


# ---- 适配器：doc-doc 的相似度 ----

def test_pairwise_scores_stored_vectors_against_each_other():
    """doc-doc 只能单独算：rank 会按查询侧口径编码（BGE 中文模型要加非对称前缀），
    两端都是文档时那个前缀就是噪声。"""
    matrix = cosine_matrix([_vector(1, 0), _vector(0.6, 0.8), _vector(0, 1)])

    assert matrix[0][1] == pytest.approx(0.6, abs=1e-6)
    assert matrix[0][2] == pytest.approx(0.0, abs=1e-6)
    assert matrix[1][0] == pytest.approx(matrix[0][1]) and matrix[0][0] == pytest.approx(1.0)


def test_pairwise_normalises_whatever_the_provider_stored():
    """云端服务不保证归一化，同方向不同长度的两个向量仍然是余弦 1。"""
    assert cosine_matrix([_vector(3, 0), _vector(9, 0)])[0][1] == pytest.approx(1.0)


def test_pairwise_gives_up_when_the_stored_dimensions_disagree():
    """换过嵌入模型又没重建索引。返回 None 让调用方降级，别给出错的相似度。
    768 与 1024 这种真实组合按字节数正好整除，只靠 reshape 报错是拦不住的。"""
    assert cosine_matrix([_vector(1, 0), _vector(1, 0, 0)]) is None
    assert cosine_matrix([_vector(*[1.0] * 768), _vector(*[1.0] * 1024)]) is None
    assert cosine_matrix([]) == []


def test_pairwise_survives_an_all_zero_vector():
    """空正文编出来的零向量：长度为 0 不能拿去除，否则整张矩阵变成 nan，谁跟谁都比不出来。"""
    matrix = cosine_matrix([_vector(0, 0), _vector(1, 0)])

    assert matrix[0][1] == 0.0 and matrix[1][1] == pytest.approx(1.0)


def test_the_embedder_pairs_documents_without_loading_a_model():
    """向量已经在库里，这一步只是解字节串做点积——不该为它把模型拉起来。"""
    embedder = BgeEmbedder(model_name="never-loaded")

    assert embedder.pairwise([_vector(1, 0), _vector(0, 1)])[0][1] == pytest.approx(0.0)
    assert embedder.status()["loaded"] is False


# ---- 集成：向量 → 边 → 接口 ----

# 两本教材各三页，外加课程首页。a1 与 b1 讲同一件事（余弦 0.99），其余各讲各的。
# 首页的向量和 a1 一样，它要是没被摘掉，最强的那条边就会是它。
_PAGES = [
    ("index", "课程总览", "", "m1", (1, 0, 0, 0, 0)),
    ("a0", "调度总览", "", "m1", (0, 0, 0, 1, 0)),
    ("a1", "先来先服务", "a0", "m1", (1, 0, 0, 0, 0)),
    ("a2", "时间片轮转", "a0", "m1", (0.6, 0.8, 0, 0, 0)),
    ("b0", "进程管理", "", "m2", (0, 0, 0, 0, 1)),
    ("b1", "FIFO 调度", "b0", "m2", (0.99, 0.141, 0, 0, 0)),
    ("b2", "内存分页", "b0", "m2", (0.5, 0, 0.866, 0, 0)),
]


def _seed_pages(wiki_store: WikiStore, repository: KnowledgeRepository, *,
                course_id: str, materials: dict[str, str]) -> None:
    """六页写两份：磁盘上那份给出层级（配对要顺 parent 链），检索库那份带手造向量。"""
    rows = []
    for concept_id, name, parent, key, _vector_values in _PAGES:
        wiki_store.write(course_id=course_id, concept_id=concept_id, concept_name=name,
                         body=f"{name}的整理稿。", source_hash="h", source_refs=[],
                         updated_at="2026-08-06T00:00:00Z", material_id=materials[key],
                         parent_id=parent or None, level=1 if parent else 0, order=len(rows))
        rows.append({"concept_id": concept_id, "concept_name": name,
                     "material_id": materials[key], "content": name})
    repository.replace_wiki_chunks(course_id=course_id, pages=rows,
                                   embeddings=[_vector(*values) for *_rest, values in _PAGES])


@pytest.fixture
def paired(tmp_path):
    """真的 KnowledgeService，两本教材各三页知识页。"""
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
    materials = {
        key: repository.create_material(course_id=course.id, filename=filename,
                                        storage_path=data_dir / filename, mime_type="text/markdown",
                                        byte_size=10).id
        for key, filename in (("m1", "os-lecture.md"), ("m2", "os-textbook.md"))
    }
    wiki_store = WikiStore(settings.data_dir)
    service = KnowledgeService(repository=repository, settings=settings, wiki_store=wiki_store,
                               wiki_is_enabled=lambda _course_id: True, embedder=VectorMath())
    _seed_pages(wiki_store, repository, course_id=course.id, materials=materials)
    return service, course.id


def test_the_same_section_in_two_textbooks_is_paired(paired):
    """这件功能的全部意义：两本书讲同一节时，读其中一页能看见另一本也讲了。"""
    service, course_id = paired

    edges = service.wiki_pairs(course_id=course_id)

    assert {edges[0]["a"], edges[0]["b"]} == {"a1", "b1"} and edges[0]["score"] > 0.9
    assert edges[0]["a_name"] == "先来先服务" and edges[0]["b_name"] == "FIFO 调度"
    assert edges[0]["a_document"] == "os-lecture.md" and edges[0]["b_document"] == "os-textbook.md"


def test_every_edge_joins_two_different_textbooks(paired):
    """同一本书里的页一条都不连（a1/a2 是兄弟，a0 是它们的章节页）；
    首页转述的是整门课、和谁都像，也摘掉。"""
    service, course_id = paired

    edges = service.wiki_pairs(course_id=course_id)
    pairs = {frozenset({str(edge["a"]), str(edge["b"])}) for edge in edges}

    assert edges and all({str(edge["a"])[0], str(edge["b"])[0]} == {"a", "b"} for edge in edges)
    assert frozenset({"a1", "a2"}) not in pairs and frozenset({"a0", "a1"}) not in pairs
    assert all("index" not in pair for pair in pairs)


def test_the_two_chapter_pages_that_share_nothing_are_left_alone(paired):
    """两本书的章节页各讲各的（余弦 0）。页数不到 k 时人人互为近邻、门槛也压不住它们，
    最后拦下这条边的是「余弦要为正」那道线。"""
    service, course_id = paired

    pairs = {frozenset({str(edge["a"]), str(edge["b"])}) for edge in service.wiki_pairs(course_id=course_id)}

    assert frozenset({"a0", "b0"}) not in pairs
    assert all(edge["score"] > 0 for edge in service.wiki_pairs(course_id=course_id))


def test_without_an_embedder_there_are_no_pairs(paired):
    """demo 与没配嵌入模型的机器：返回空表，不做词面兜底——两套排序会给出两种结论。"""
    service, course_id = paired
    service._embedder = None

    assert service.wiki_pairs(course_id=course_id) == []


def test_an_embedder_that_cannot_pair_degrades_to_no_pairs(paired):
    """库里维度对不上（换过嵌入模型又没重建索引），或者接了个不做 doc-doc 的旧适配器：
    配对是页面上的旁注，它取不到不该让整份页面清单打不开。"""
    service, course_id = paired

    class Mismatched(VectorMath):
        def pairwise(self, vectors: list[bytes]) -> list[list[float]] | None:
            return None

    service._embedder = Mismatched()
    assert service.wiki_pairs(course_id=course_id) == []

    service._embedder = OldPort()
    assert service.wiki_pairs(course_id=course_id) == []


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


def _upload(client: TestClient, course_id: str, filename: str) -> str:
    material = client.post(f"/api/v2/courses/{course_id}/materials",
                           files={"file": (filename, "# 调度\n\n先来先服务会产生护航效应。\n", "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if client.get(f"/api/v2/jobs/{job['id']}").json()["status"] in {"completed", "failed"}:
            return material["id"]
        time.sleep(0.01)
    pytest.fail("索引没有进入终态")


def test_the_graph_route_returns_the_edges_of_a_real_course(client):
    """路由要排在 /wiki/{concept_id} 前面，否则 graph 会被当成概念 id 查页，静默 404。
    先造出真的边再断言：空表这条判据在路由接错时也会过。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    materials = {key: _upload(client, course["id"], filename)
                 for key, filename in (("m1", "os-lecture.md"), ("m2", "os-textbook.md"))}
    space = workspace(client)
    space.knowledge._embedder = VectorMath()
    _seed_pages(space.knowledge._wiki, space.knowledge._repository,
                course_id=course["id"], materials=materials)

    response = client.get(f"/api/v2/courses/{course['id']}/wiki/graph")

    assert response.status_code == 200, response.text
    edges = response.json()["edges"]
    assert {edges[0]["a"], edges[0]["b"]} == {"a1", "b1"}
    assert {edges[0]["a_document"], edges[0]["b_document"]} == {"os-lecture.md", "os-textbook.md"}


def test_the_graph_route_is_empty_without_an_embedder(client):
    """demo 实例照样要 200：没有向量是「没有边」，不是错误。"""
    course = client.post("/api/v2/courses", json={"name": "大学物理"}).json()

    response = client.get(f"/api/v2/courses/{course['id']}/wiki/graph")

    assert response.status_code == 200 and response.json() == {"edges": []}


def test_the_graph_route_404s_for_an_unknown_course(client):
    assert client.get("/api/v2/courses/course_nope/wiki/graph").status_code == 404
