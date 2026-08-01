"""只差大小写的概念名：id 由 casefold 派生，两个变体本来就是同一个概念。

以前它们会撞 `concepts.id` 主键，整个索引作业直接失败。这里守住合并行为，
以及合并不能碰掉挂在这个 id 上的掌握度与错题历史。
"""
from __future__ import annotations

from core.store import SQLiteStore
from modules.knowledge.concepts import concept_id_for
from modules.knowledge.repository import KnowledgeRepository
from modules.learning.repository import LearningRepository
from modules.learning.service import LearningService


def _workspace(tmp_path, *, materials: tuple[str, ...] = ("m1",)):
    store = SQLiteStore(tmp_path / "cp.db")
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id,name,color,created_at,updated_at) VALUES ('course_x','测试课','#B56E3D','now','now')")
        for material_id in materials:
            conn.execute(
                "INSERT INTO materials(id,course_id,filename,storage_path,mime_type,byte_size,index_status,created_at,updated_at)"
                " VALUES (?,'course_x','llm.pdf','/tmp/llm.pdf','text/plain',1,'indexed','now','now')", (material_id,))
    knowledge = KnowledgeRepository.__new__(KnowledgeRepository)
    knowledge._store = store
    return knowledge, LearningService(LearningRepository(store)), store


def _rows(store) -> list[tuple]:
    with store.read() as conn:
        return [tuple(row) for row in conn.execute("SELECT id, name, material_id, page, mention_count FROM concepts ORDER BY id")]


def test_case_variants_in_one_batch_become_one_concept(tmp_path):
    """同一批候选里同时抽到 Attention 和 attention，以前直接抛 UNIQUE 主键冲突。"""
    knowledge, _service, store = _workspace(tmp_path)

    total = knowledge.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "Attention", "page": 1, "mention_count": 5},
                    {"name": "attention", "page": 7, "mention_count": 2}],
    )

    assert total == 1
    assert _rows(store) == [(concept_id_for("course_x", "Attention"), "Attention", "m1", 1, 5)]


def test_the_more_mentioned_variant_supplies_the_display_name(tmp_path):
    """显示名取提及次数多的那个变体，与候选在列表里的先后无关。"""
    knowledge, _service, store = _workspace(tmp_path)

    knowledge.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "lora", "page": 9, "mention_count": 1},
                    {"name": "LoRA", "page": 3, "mention_count": 6}],
    )

    assert _rows(store) == [(concept_id_for("course_x", "LoRA"), "LoRA", "m1", 3, 6)]


def test_a_variant_merges_into_a_concept_already_in_the_database(tmp_path):
    """增量索引：库里已有 Attention，这一轮只抽到 attention。批内去重挡不住这条路。"""
    knowledge, _service, store = _workspace(tmp_path)
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                        candidates=[{"name": "Attention", "page": 1, "mention_count": 5}])

    total = knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                                candidates=[{"name": "attention", "page": 7, "mention_count": 2}])

    # 本教材重新索引以这次为准；已入库的显示名不改。
    assert total == 1
    assert _rows(store) == [(concept_id_for("course_x", "Attention"), "Attention", "m1", 7, 2)]


def test_a_variant_from_another_material_does_not_steal_the_concept(tmp_path):
    """另一本教材抽到小写变体时不抢归属，次数取较大值——与同名概念的既有口径一致。"""
    knowledge, _service, store = _workspace(tmp_path, materials=("m1", "m2"))
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                        candidates=[{"name": "Attention", "page": 1, "mention_count": 5}])

    knowledge.replace_material_concepts(course_id="course_x", material_id="m2",
                                        candidates=[{"name": "attention", "page": 7, "mention_count": 2}])

    assert _rows(store) == [(concept_id_for("course_x", "Attention"), "Attention", "m1", 1, 5)]


def test_merging_a_variant_keeps_mastery_and_mistake_history(tmp_path):
    """id 没变，挂在它上面的掌握度与错题记录就得原样还在。"""
    knowledge, service, store = _workspace(tmp_path)
    concept = concept_id_for("course_x", "Attention")
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                        candidates=[{"name": "Attention", "page": 1, "mention_count": 5}])
    service.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept)
    assert [item.concept_id for item in service.mistakes(course_id="course_x")] == [concept]

    knowledge.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "attention", "page": 7, "mention_count": 2}, {"name": "Attention", "page": 1, "mention_count": 3}],
    )

    assert [(item.concept_id, item.wrong_count) for item in service.mistakes(course_id="course_x")] == [(concept, 1)]
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM concept_mastery WHERE concept_id = ?", (concept,)).fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reindexing_the_same_candidates_is_idempotent(tmp_path):
    """同一份教材重复索引结果必须一致——纯规则抽取的既有承诺。"""
    knowledge, _service, store = _workspace(tmp_path)
    candidates = [{"name": "Attention", "page": 1, "mention_count": 5},
                  {"name": "attention", "page": 7, "mention_count": 2},
                  {"name": "LoRA", "page": 3, "mention_count": 4}]

    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=candidates)
    first = _rows(store)
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=candidates)

    assert _rows(store) == first
    assert len(first) == 2
