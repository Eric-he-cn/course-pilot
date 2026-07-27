"""中文词面检索（BM25 那一路）。

FTS5 默认的 unicode61 把整段中文当成一个 token：入库「梯度下降是最常用的优化算法」
之后查「梯度下降」是 0 命中，只有逐字复述整句才匹配得上。chunks_fts 因此改用 trigram
分词器，查询侧也按三字滑窗切开。这里直接打 FTS 那条 SQL，绕开 LIKE 兜底，
确保断言的是词面这一路本身。
"""
from __future__ import annotations

import pytest

from core.store import SQLiteStore
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.repository import KnowledgeRepository

PAGES = [
    "梯度下降是最常用的优化算法，步长过大会在极小值附近震荡。",
    "反向传播利用链式法则逐层求出各参数的梯度。",
    "Gradient descent is the most common optimizer; a large step size oscillates.",
    "Batch normalization stabilizes training by normalizing layer activations.",
]


@pytest.fixture
def repository(tmp_path):
    store = SQLiteStore(tmp_path / "coursepilot.db")
    store.migrate()
    course = CourseService(CourseRepository(store)).create_course(name="深度学习")
    repository = KnowledgeRepository(store)
    material = repository.create_material(
        course_id=course.id, filename="dl.md", storage_path=tmp_path / "dl.md",
        mime_type="text/markdown", byte_size=1,
    )
    repository.replace_chunks(
        material_id=material.id, course_id=course.id,
        chunks=[(index + 1, text) for index, text in enumerate(PAGES)],
    )
    repository.course_id = course.id  # type: ignore[attr-defined]
    return repository


def _fts_hits(repository, query: str) -> list[str]:
    """只走 FTS MATCH，不落 LIKE 兜底——兜底能命中不代表 BM25 在工作。"""
    import re

    tokens = [token for token in re.findall(r"[^\W_一-鿿]+|[一-鿿]+", query, flags=re.UNICODE) if token]
    expression = " OR ".join(f'"{term}"' for term in KnowledgeRepository._fts_terms(tokens))
    with repository._store.read() as conn:
        rows = conn.execute(
            "SELECT c.content FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id "
            "WHERE chunks_fts.course_id = ? AND chunks_fts MATCH ? ORDER BY bm25(chunks_fts)",
            (repository.course_id, expression),
        ).fetchall()
    return [row["content"] for row in rows]


@pytest.mark.parametrize("query, expected", [
    ("梯度下降", "优化算法"),
    ("链式法则", "反向传播"),
    ("优化算法", "梯度下降"),
    ("链式法则怎么用", "反向传播"),          # 整句提问，不是干净的术语
    ("gradient descent", "Gradient descent"),  # 英文不能因为换分词器而退化
    ("normalization", "normalization"),
])
def test_lexical_search_finds_the_right_passage(repository, query, expected):
    hits = _fts_hits(repository, query)
    assert hits, f"「{query}」在词面这一路 0 命中"
    assert expected in hits[0], f"「{query}」排第一的是：{hits[0]}"


def test_unrelated_query_matches_nothing(repository):
    assert _fts_hits(repository, "红烧肉怎么做") == []


def test_cjk_runs_are_split_into_trigrams():
    """英文词整体保留；中文长串切成三字滑窗；不足三字的原样留给 LIKE 兜底。"""
    assert KnowledgeRepository._fts_terms(["链式法则"]) == ["链式法", "式法则"]
    assert KnowledgeRepository._fts_terms(["gradient"]) == ["gradient"]
    # 不足三字的词在 trigram 索引里注定 0 命中，剔掉交给 LIKE 兜底，中英同理。
    assert KnowledgeRepository._fts_terms(["步长"]) == []
    assert KnowledgeRepository._fts_terms(["AI", "极限"]) == []
    assert KnowledgeRepository._fts_terms(["AI", "梯度下降"]) == ["梯度下", "度下降"]


def test_short_only_query_falls_back_instead_of_erroring(repository):
    """全是短词时 FTS 表达式为空。这一路没得查，但检索本身不能因此报错或返回空。"""
    hits = repository.search(course_id=repository.course_id, query="极限 AI", limit=5)
    assert isinstance(hits, list)
    steps = repository.search(course_id=repository.course_id, query="步长", limit=5)
    assert steps and "步长" in steps[0].content, "两字词该由 LIKE 兜底命中"


def test_migration_rebuilds_the_index_from_existing_chunks(tmp_path):
    """迁移 19 是 DROP 后重建：已有库里的 chunk 必须原样回到 FTS 里，
    否则老用户升上来会变成检索不到任何东西，而且没有任何报错。"""
    import sqlite3

    from core.store import MIGRATIONS

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:  # 停在迁移 18 的老库
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        for version, sql in MIGRATIONS:
            if version >= 19:
                break
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
        conn.execute("INSERT INTO courses(id, name, color, created_at, updated_at) VALUES ('c1','旧课','#000','t','t')")
        conn.execute("INSERT INTO materials(id, course_id, filename, storage_path, mime_type, byte_size, index_status, created_at, updated_at) "
                     "VALUES ('m1','c1','old.md','old.md','text/markdown',1,'indexed','t','t')")
        conn.execute("INSERT INTO chunks(id, material_id, course_id, ordinal, page, content) VALUES ('k1','m1','c1',0,1,?)", (PAGES[0],))
        conn.commit()

    store = SQLiteStore(path)
    store.migrate()
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 1, "迁移后 FTS 是空的"
        hit = conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", ('"梯度下降"',)).fetchone()[0]
    assert hit == 1, "重建后的索引查不到中文"
