from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.store import SQLiteStore
from modules.knowledge.concepts import concept_id_for
from modules.learning.mistakes import GRADUATE_STREAK, replay_mistakes
from modules.learning.repository import LearningRepository
from modules.learning.service import LearningService

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
CONCEPT = concept_id_for("course_x", "链式法则")
OTHER = concept_id_for("course_x", "洛必达法则")


def _event(kind: str, day: int, **payload) -> dict:
    return {"kind": kind, "created_at": (BASE + timedelta(days=day)).isoformat(), "payload": payload}


def _at(day: int) -> str:
    return (BASE + timedelta(days=day)).isoformat()


# --- 纯函数状态机 ---------------------------------------------------------


def test_first_wrong_opens_a_record_and_later_wrongs_accumulate():
    first = replay_mistakes([_event("attempt_incorrect", 0)])
    assert (first.status, first.wrong_count, first.streak, first.relapse_count) == ("active", 1, 0, 0)
    assert first.first_wrong_at == first.last_wrong_at == _at(0)
    assert first.graduated_at is None

    # 中间答对一次让 streak 涨到 1，再错要把它打回 0 且错次加一。
    again = replay_mistakes([_event("attempt_incorrect", 0), _event("attempt_correct", 1), _event("attempt_incorrect", 2)])
    assert (again.status, again.wrong_count, again.streak) == ("active", 2, 0)
    assert again.first_wrong_at == _at(0) and again.last_wrong_at == _at(2)


def test_never_wrong_means_no_record():
    assert replay_mistakes([_event("attempt_correct", 0), _event("attempt_correct", 1)]) is None
    assert replay_mistakes([]) is None


def test_two_correct_in_a_row_graduates():
    one = replay_mistakes([_event("attempt_incorrect", 0), _event("attempt_correct", 1)])
    assert (one.status, one.streak, one.graduated_at) == ("active", 1, None)  # 一次还不够

    two = replay_mistakes([_event("attempt_incorrect", 0), _event("attempt_correct", 1), _event("attempt_correct", 2)])
    assert (two.status, two.streak, two.wrong_count) == ("graduated", GRADUATE_STREAK, 1)
    assert two.graduated_at == _at(2)


def test_relapse_returns_to_active_and_clears_graduation():
    stream = [_event("attempt_incorrect", 0), _event("attempt_correct", 1), _event("attempt_correct", 2),
              _event("attempt_incorrect", 5)]
    relapsed = replay_mistakes(stream)
    assert relapsed.status == "active"
    assert relapsed.relapse_count == 1
    assert relapsed.graduated_at is None  # 复发时清空
    assert relapsed.wrong_count == 2      # 累计错次毕业不清零
    assert relapsed.streak == 0
    assert relapsed.last_wrong_at == _at(5)

    # 再毕业再错：relapse_count 继续累加。
    twice = replay_mistakes(stream + [_event("attempt_correct", 6), _event("attempt_correct", 7), _event("attempt_incorrect", 8)])
    assert (twice.status, twice.relapse_count, twice.wrong_count) == ("active", 2, 3)


def test_alternating_correct_and_wrong_never_graduates():
    stream = []
    for day in range(0, 12, 2):
        stream += [_event("attempt_incorrect", day), _event("attempt_correct", day + 1)]
    state = replay_mistakes(stream)
    # 每次答错都把 streak 归零，单次答对攒不到阈值。
    assert state.status == "active"
    assert state.streak == 1
    assert state.wrong_count == 6
    assert state.relapse_count == 0


def test_auxiliary_kinds_do_not_enter_the_projection():
    # 追问与用户标记不算客观证据，夹在中间也不能打断连对。
    assert replay_mistakes([_event("follow_up", 0), _event("user_override", 1)]) is None
    state = replay_mistakes([_event("attempt_incorrect", 0), _event("attempt_correct", 1),
                             _event("user_override", 2), _event("follow_up", 3), _event("attempt_correct", 4)])
    assert state.status == "graduated"
    assert state.streak == GRADUATE_STREAK


# --- 落库接线 -------------------------------------------------------------


@pytest.fixture
def learning(tmp_path):
    store = SQLiteStore(tmp_path / "coursepilot.db")
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id, name, color, created_at, updated_at) VALUES ('course_x', '测试课', '#B56E3D', 'now', 'now')")
        for concept_id, name in ((CONCEPT, "链式法则"), (OTHER, "洛必达法则")):
            conn.execute("INSERT INTO concepts(id, course_id, name, mention_count, created_at) VALUES (?, 'course_x', ?, 3, 'now')", (concept_id, name))
    return LearningService(LearningRepository(store))


def _mistake(learning: LearningService, concept_id: str = CONCEPT):
    return next(item for item in learning.mistakes(course_id="course_x") if item.concept_id == concept_id)


def test_incremental_wiring_matches_a_single_replay(learning):
    """逐条写入攒出来的行，必须等于同一串事件一次重放的结果。"""
    kinds = ("attempt_incorrect", "attempt_correct", "attempt_correct", "attempt_incorrect", "attempt_correct")
    for kind in kinds:
        learning.record_evidence(course_id="course_x", kind=kind, concept_id=CONCEPT)

    rows = learning._repository.concept_event_rows(CONCEPT)
    expected = replay_mistakes(rows)
    got = _mistake(learning)
    assert (got.status, got.wrong_count, got.streak, got.relapse_count) == (
        expected.status, expected.wrong_count, expected.streak, expected.relapse_count)
    assert (got.first_wrong_at, got.last_wrong_at, got.graduated_at) == (
        expected.first_wrong_at, expected.last_wrong_at, expected.graduated_at)
    # 这串事件确实走完了「毕业又复发」，否则上面的相等是空对空。
    assert (expected.relapse_count, expected.status) == (1, "active")


def test_only_wrong_answers_create_rows(learning):
    learning.record_evidence(course_id="course_x", kind="attempt_correct", concept_id=OTHER)
    learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=CONCEPT)
    assert [item.concept_id for item in learning.mistakes(course_id="course_x")] == [CONCEPT]


def test_archive_exposes_active_first_and_graduated_count(learning):
    for kind in ("attempt_incorrect", "attempt_correct", "attempt_correct"):
        learning.record_evidence(course_id="course_x", kind=kind, concept_id=OTHER)  # 毕业
    learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=CONCEPT)  # 活跃

    archive = learning.get_archive(course_id="course_x")
    assert archive.graduated_count == 1
    assert [item.status for item in archive.mistakes] == ["active", "graduated"]  # active 优先
    assert archive.mistakes[0].concept_id == CONCEPT


def test_archive_backfills_when_the_table_is_empty(learning):
    for kind in ("attempt_incorrect", "attempt_correct"):
        learning.record_evidence(course_id="course_x", kind=kind, concept_id=CONCEPT)
    # 模拟错题表上线前就已存在的事件流。
    with learning._repository._store.write() as conn:
        conn.execute("DELETE FROM mistake_records")
    assert learning.mistakes(course_id="course_x") == []

    archive = learning.get_archive(course_id="course_x")
    assert [item.concept_id for item in archive.mistakes] == [CONCEPT]
    assert (archive.mistakes[0].wrong_count, archive.mistakes[0].streak) == (1, 1)


def test_rebuild_skips_deleted_concepts(learning):
    learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=CONCEPT)
    learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=OTHER)
    # 概念被删掉，但事件流留着（evidence_events.concept_id 没有外键）。投影按真实删除顺序清，
    # 不手动预清错题表——那样会把「级联漏了这张表」这类问题挡在测试之外。
    with learning._repository._store.write() as conn:
        for table in ("concept_mastery", "mistake_records", "concept_aliases"):
            conn.execute(f"DELETE FROM {table} WHERE concept_id = ?", (OTHER,))
        conn.execute("DELETE FROM concepts WHERE id = ?", (OTHER,))

    assert learning.rebuild(course_id="course_x") == 1  # 已删概念不参与，也不撞外键
    assert [item.concept_id for item in learning.mistakes(course_id="course_x")] == [CONCEPT]


def test_backfill_runs_even_when_a_new_mistake_landed_first(learning):
    """回填闸门不能看表空不空：新错题先落一行，历史错题也必须补出来。"""
    learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=CONCEPT)
    # 抹掉投影与完成标记，只留事件流，模拟错题表上线前的历史。
    with learning._repository._store.write() as conn:
        conn.execute("DELETE FROM mistake_records")
        conn.execute("DELETE FROM mistake_backfills")
    # 然后用户在新练习里答错另一个概念，表里于是先有了一行。
    learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=OTHER)
    assert [item.concept_id for item in learning.mistakes(course_id="course_x")] == [OTHER]

    names = {item.concept_id for item in learning.get_archive(course_id="course_x").mistakes}
    assert names == {CONCEPT, OTHER}, "历史错题必须跟着回填一起出现"


def test_backfill_stops_after_one_pass_even_with_nothing_to_write(learning):
    """从没答错过的课程，回填不能每次读档案都白跑一遍。"""
    learning.record_evidence(course_id="course_x", kind="attempt_correct", concept_id=CONCEPT)
    assert learning.get_archive(course_id="course_x").mistakes == []
    assert learning._repository.mistake_backfill_done(course_id="course_x")

    calls: list[str] = []
    original = learning._repository.projectable_concept_ids
    learning._repository.projectable_concept_ids = lambda **kw: calls.append(kw["course_id"]) or original(**kw)
    learning.get_archive(course_id="course_x")
    learning.get_archive(course_id="course_x")
    assert calls == [], "标记已落，不该再扫一遍事件流"


def test_archive_limits_are_independent(learning):
    for concept in (CONCEPT, OTHER):
        learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept)
    # 只想少看几条事件时，错题条数不该被一起砍掉。
    archive = learning.get_archive(course_id="course_x", limit=1)
    assert len(archive.events) == 1
    assert len(archive.mistakes) == 2
    assert len(learning.get_archive(course_id="course_x", mistake_limit=1).mistakes) == 1


def _seeded(tmp_path, name: str) -> LearningService:
    store = SQLiteStore(tmp_path / name)
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id, name, color, created_at, updated_at) VALUES ('course_x', '测试课', '#B56E3D', 'now', 'now')")
        conn.execute("INSERT INTO concepts(id, course_id, name, mention_count, created_at) VALUES (?, 'course_x', '链式法则', 3, 'now')", (CONCEPT,))
    service = LearningService(LearningRepository(store))
    # 时间戳写死：掌握度的 last_reviewed_at / due_at 由事件时间推出，用 utc_now 两库会差几微秒。
    for index, kind in enumerate(("attempt_incorrect", "attempt_correct", "attempt_correct", "attempt_incorrect", "attempt_correct")):
        service._repository.insert_event(
            event_id=f"evidence_{index}", course_id="course_x", concept_id=CONCEPT,
            attribution_status="attributed", topic_hint=None, kind=kind, payload={}, timestamp=_at(index),
        )
    return service


MASTERY_COLUMNS = ("concept_id", "course_id", "bkt_p", "fsrs_stability", "fsrs_difficulty",
                   "objective_events", "last_reviewed_at", "due_at", "algorithm_version")


def test_reindex_keeps_surviving_concepts_and_drops_vanished_ones(tmp_path):
    """重新索引只该清掉这次没再抽到的概念；还在的概念保住 id，错题历史不断档。"""
    from modules.knowledge.repository import KnowledgeRepository

    store = SQLiteStore(tmp_path / "cp.db")
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id,name,color,created_at,updated_at) VALUES ('course_x','测试课','#B56E3D','now','now')")
        conn.execute("INSERT INTO materials(id,course_id,filename,storage_path,mime_type,byte_size,index_status,created_at,updated_at)"
                     " VALUES ('m1','course_x','os.pdf','/tmp/os.pdf','text/plain',1,'indexed','now','now')")
    knowledge = KnowledgeRepository.__new__(KnowledgeRepository)
    knowledge._store = store
    service = LearningService(LearningRepository(store))

    knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                        candidates=[{"name": "链式法则", "page": 1}, {"name": "洛必达法则", "page": 2}])
    for name in ("链式法则", "洛必达法则"):
        service.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept_id_for("course_x", name))
    assert len(service.mistakes(course_id="course_x")) == 2

    # 第二遍只抽到链式法则，页码也变了。
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=[{"name": "链式法则", "page": 9}])

    assert [item.name for item in service.mistakes(course_id="course_x")] == ["链式法则"]
    with store.read() as conn:
        assert conn.execute("SELECT page FROM concepts WHERE name = '链式法则'").fetchone()[0] == 9
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _knowledge_and_learning(tmp_path, name: str = "cp.db"):
    from modules.knowledge.repository import KnowledgeRepository

    store = SQLiteStore(tmp_path / name)
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id,name,color,created_at,updated_at) VALUES ('course_x','测试课','#B56E3D','now','now')")
        conn.execute("INSERT INTO materials(id,course_id,filename,storage_path,mime_type,byte_size,index_status,created_at,updated_at)"
                     " VALUES ('m1','course_x','os.pdf','/tmp/os.pdf','text/plain',1,'indexed','now','now')")
    knowledge = KnowledgeRepository.__new__(KnowledgeRepository)
    knowledge._store = store
    return knowledge, LearningService(LearningRepository(store)), store


def test_history_survives_a_concept_disappearing_and_coming_back(tmp_path):
    """概念被删掉时投影跟着删，而 id 由课程 + 名字派生，重新抽到还是同一个。

    关键是概念不在的那段时间里**读过一次档案**：那次自愈会把标记落回去，
    所以真正要触发重算的时刻是概念回来，不是它离开。
    """
    knowledge, service, store = _knowledge_and_learning(tmp_path)
    concept = concept_id_for("course_x", "链式法则")

    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=[{"name": "链式法则", "page": 1}])
    service.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept)
    service.record_evidence(course_id="course_x", kind="attempt_correct", concept_id=concept)
    service.get_archive(course_id="course_x")  # 标记落下

    # 这一轮没抽到它：概念与两张投影一起没了。
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=[{"name": "洛必达法则", "page": 2}])
    assert service.mistakes(course_id="course_x") == []
    # 概念还没回来就读一次档案：标记被重新落上。
    assert service.get_archive(course_id="course_x").mistakes == []
    assert service._repository.mistake_backfill_done(course_id="course_x")

    # 又被抽出来了，id 与之前相同。这一轮**一个概念都没删掉**（洛必达法则也留着），
    # 所以「删掉东西才清标记」那种写法在这里不会清，历史就永久回不来。
    knowledge.replace_material_concepts(
        course_id="course_x", material_id="m1",
        candidates=[{"name": "链式法则", "page": 1}, {"name": "洛必达法则", "page": 2}],
    )
    archive = service.get_archive(course_id="course_x")

    assert [(item.concept_id, item.wrong_count, item.streak) for item in archive.mistakes] == [(concept, 1, 1)]
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM concept_mastery WHERE concept_id = ?", (concept,)).fetchone()[0] == 1


def test_deleting_a_material_lets_a_re_upload_recover_history(tmp_path):
    """重新上传同一份文件会算出同样的概念 id，删教材也要清掉标记。"""
    knowledge, service, store = _knowledge_and_learning(tmp_path)
    concept = concept_id_for("course_x", "链式法则")
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=[{"name": "链式法则", "page": 1}])
    service.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept)
    service.get_archive(course_id="course_x")

    from modules.knowledge.repository import _purge_materials

    with store.write() as conn:
        _purge_materials(conn, ["m1"])
    # 重新上传之前先读一次档案：标记落回，删教材时清的那一下就白清了。
    assert service.get_archive(course_id="course_x").mistakes == []
    assert service._repository.mistake_backfill_done(course_id="course_x")

    # 重新上传并索引，概念 id 与之前相同。
    with store.write() as conn:
        conn.execute("INSERT INTO materials(id,course_id,filename,storage_path,mime_type,byte_size,index_status,created_at,updated_at)"
                     " VALUES ('m2','course_x','os.pdf','/tmp/os.pdf','text/plain',1,'indexed','now','now')")
    knowledge.replace_material_concepts(course_id="course_x", material_id="m2", candidates=[{"name": "链式法则", "page": 1}])
    assert [item.wrong_count for item in service.get_archive(course_id="course_x").mistakes] == [1]
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM concept_mastery WHERE concept_id = ?", (concept,)).fetchone()[0] == 1


def test_empty_extraction_leaves_existing_concepts_alone(tmp_path):
    """抽取为空更像抽取失败，不该被当成"这本教材的概念都没了"。"""
    knowledge, service, store = _knowledge_and_learning(tmp_path)
    concept = concept_id_for("course_x", "链式法则")
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=[{"name": "链式法则", "page": 1}])
    service.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept)

    assert knowledge.replace_material_concepts(course_id="course_x", material_id="m1", candidates=[]) == 1

    assert [item.concept_id for item in service.mistakes(course_id="course_x")] == [concept]
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM concepts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM concept_mastery").fetchone()[0] == 1


def test_counts_stay_totals_when_the_mistake_page_is_truncated(learning):
    """错题列表是一页，两个计数是总数：活跃条目占满一页时毕业的排不上，计数仍要说实话。"""
    with learning._repository._store.write() as conn:
        for index in range(4):
            name = f"概念{index}"
            conn.execute("INSERT INTO concepts(id, course_id, name, mention_count, created_at) VALUES (?, 'course_x', ?, 1, 'now')",
                         (concept_id_for("course_x", name), name))
    for index in range(4):
        learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id=concept_id_for("course_x", f"概念{index}"))
    for kind in ("attempt_incorrect", "attempt_correct", "attempt_correct"):
        learning.record_evidence(course_id="course_x", kind=kind, concept_id=CONCEPT)  # 一个毕业的

    archive = learning.get_archive(course_id="course_x", mistake_limit=2)
    assert len(archive.mistakes) == 2
    assert all(item.status == "active" for item in archive.mistakes)  # 活跃优先，毕业的排不上
    assert (archive.active_count, archive.graduated_count) == (4, 1)  # 计数不受 limit 影响


def test_archive_tool_text_says_how_many_mistakes_it_left_out():
    """列表被截断时模型必须看得到「还有多少条没列」，否则它会当成全部。"""
    from contracts.knowledge import ResolvedKnowledgeScope
    from modules.agent.tools import ToolExecutor
    from modules.learning.models import ArchiveSummary, EvidenceEvent, MistakeRecord

    shown = [MistakeRecord(f"concept_{i}", f"概念{i}", "active", 2, 0, "t0", "t1", None, 0) for i in range(3)]
    summary = ArchiveSummary(
        course_id="course_x", evidence_count=99,
        events=[EvidenceEvent("e1", "course_x", "concept_0", "attributed", None, "attempt_incorrect", "t1")],
        mistakes=shown, active_count=25, graduated_count=4,
    )

    class _Stub:
        def get_archive(self, **_kwargs):
            return summary

    executor = object.__new__(ToolExecutor)
    executor._archive = _Stub()
    text = executor._archive_events(ResolvedKnowledgeScope(turn_id="t1", course_id="course_x", resolver_version="v1")).text
    assert "活跃错题 25 个" in text
    assert "已毕业 4 个" in text
    assert "另有 22 个活跃错题未列出" in text


def test_reindex_takes_the_new_mention_count_but_never_undercuts_another_material(tmp_path):
    """本教材重新索引以这次抽取结果为准，次数下降要真的降下来；
    同名概念属于本课程另一份教材时，不能被这次的小值盖掉。"""
    from modules.knowledge.repository import KnowledgeRepository

    store = SQLiteStore(tmp_path / "cp.db")
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id,name,color,created_at,updated_at) VALUES ('course_x','测试课','#B56E3D','now','now')")
        for material in ("m1", "m2"):
            conn.execute("INSERT INTO materials(id,course_id,filename,storage_path,mime_type,byte_size,index_status,created_at,updated_at)"
                         " VALUES (?,'course_x',?,?,'text/plain',1,'indexed','now','now')", (material, f"{material}.pdf", f"/tmp/{material}.pdf"))
    knowledge = KnowledgeRepository.__new__(KnowledgeRepository)
    knowledge._store = store

    def count_of(name: str) -> int:
        with store.read() as conn:
            return conn.execute("SELECT mention_count FROM concepts WHERE name = ?", (name,)).fetchone()[0]

    knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                        candidates=[{"name": "链式法则", "page": 1, "mention_count": 5}])
    assert count_of("链式法则") == 5

    # 同一份教材再索引一次，这个概念只提到 2 次了。
    knowledge.replace_material_concepts(course_id="course_x", material_id="m1",
                                        candidates=[{"name": "链式法则", "page": 1, "mention_count": 2}])
    assert count_of("链式法则") == 2, "本教材的次数下降必须落库，不能只增不减"

    # 另一份教材也提到同名概念，且提得更多：概念归属不变，次数取较大值。
    knowledge.replace_material_concepts(course_id="course_x", material_id="m2",
                                        candidates=[{"name": "链式法则", "page": 30, "mention_count": 9}])
    assert count_of("链式法则") == 9
    with store.read() as conn:
        row = conn.execute("SELECT material_id, page FROM concepts WHERE name = '链式法则'").fetchone()
    assert (row[0], row[1]) == ("m1", 1), "跨教材同名概念保持原归属与原页码"

    # 再索引 m2、次数降到 3：不能把 m1 说了算的 9 拉下来。
    knowledge.replace_material_concepts(course_id="course_x", material_id="m2",
                                        candidates=[{"name": "链式法则", "page": 30, "mention_count": 3}])
    assert count_of("链式法则") == 9


def test_mistake_projection_does_not_touch_mastery_numbers(tmp_path):
    """隔离门：两串只差错题侧处理的事件，concept_mastery 的数值列必须逐位相同。

    不含 updated_at——它每次写投影都会变，而全仓只写不读。
    """
    bare, with_mistakes = _seeded(tmp_path, "bare.db"), _seeded(tmp_path, "both.db")

    bare._project(concept_id=CONCEPT, course_id="course_x")  # 只投影掌握度
    with_mistakes._project(concept_id=CONCEPT, course_id="course_x")
    with_mistakes._project_mistake(concept_id=CONCEPT, course_id="course_x")
    with_mistakes.get_archive(course_id="course_x")  # 顺带跑回填与重建
    with_mistakes.rebuild(course_id="course_x")

    left = with_mistakes._repository.mastery_rows(course_id="course_x")[0]
    right = bare._repository.mastery_rows(course_id="course_x")[0]
    assert [left[key] for key in MASTERY_COLUMNS] == [right[key] for key in MASTERY_COLUMNS]
    # 错题侧确实产出了东西，上面的相等不是空对空。
    assert bare._repository.count_mistakes(course_id="course_x") == 0
    assert _mistake(with_mistakes).relapse_count == 1
