from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.store import SQLiteStore
from modules.knowledge.concepts import concept_id_for, extract_candidates
from modules.learning.mastery import bkt_update, mastery_score, replay
from modules.learning.repository import LearningRepository
from modules.learning.service import LearningService

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _event(kind: str, day: int, **payload) -> dict:
    return {"kind": kind, "created_at": (BASE + timedelta(days=day)).isoformat(), "payload": payload}


def test_bkt_moves_in_the_right_direction():
    assert bkt_update(0.2, correct=True) > 0.2
    assert bkt_update(0.2, correct=False) < 0.2
    # 连续答对收敛到高位但不到 1，答错不会跌破 0。
    high = 0.2
    for _ in range(6):
        high = bkt_update(high, correct=True)
    assert 0.9 < high < 1.0


def test_replay_is_deterministic_and_ignores_auxiliary_events():
    objective = [_event("attempt_incorrect", 0), _event("attempt_correct", 1), _event("attempt_correct", 2)]
    assert replay(objective) == replay(objective)  # 同一事件流必须重放出同一状态
    assert replay(objective).objective_events == 3

    # 追问与用户标记只入事件流，不参与数值更新。
    auxiliary = replay([_event("follow_up", 0), _event("user_override", 1)])
    assert auxiliary.objective_events == 0
    assert mastery_score(auxiliary, at=BASE) is None


def test_score_needs_three_objective_events_and_decays_over_time():
    state = replay([_event("attempt_correct", 0), _event("attempt_correct", 1)])
    assert mastery_score(state, at=BASE + timedelta(days=2)) is None  # 证据不足

    state = replay([_event("attempt_correct", 0), _event("attempt_correct", 1), _event("attempt_correct", 2)])
    fresh = mastery_score(state, at=BASE + timedelta(days=2))
    stale = mastery_score(state, at=BASE + timedelta(days=40))
    assert fresh is not None and stale is not None
    assert stale < fresh  # 时间衰减


@pytest.fixture
def learning(tmp_path):
    store = SQLiteStore(tmp_path / "coursepilot.db")
    store.migrate()
    with store.write() as conn:
        conn.execute("INSERT INTO courses(id, name, color, created_at, updated_at) VALUES ('course_x', '测试课', '#B56E3D', 'now', 'now')")
        conn.execute(
            "INSERT INTO concepts(id, course_id, name, mention_count, created_at) VALUES (?, 'course_x', '链式法则', 3, 'now')",
            (concept_id_for("course_x", "链式法则"),),
        )
    return LearningService(LearningRepository(store))


def test_unknown_concept_is_recorded_as_unattributed(learning):
    known = learning.record_evidence(course_id="course_x", kind="attempt_correct", concept_id=concept_id_for("course_x", "链式法则"))
    assert known.attribution_status == "attributed"

    ghost = learning.record_evidence(course_id="course_x", kind="attempt_incorrect", concept_id="concept_不存在", topic_hint="洛必达法则")
    assert ghost.attribution_status == "unattributed"
    assert ghost.concept_id is None
    # 幻觉概念不产生掌握度行，只进未归因队列。
    assert [item.name for item in learning.mastery(course_id="course_x")] == ["链式法则"]
    assert learning.get_archive(course_id="course_x").unattributed[0]["topic_hint"] == "洛必达法则"

    with pytest.raises(ValueError):
        learning.record_evidence(course_id="course_x", kind="attempt_correct")  # 既无概念也无 topic_hint
    with pytest.raises(ValueError):
        learning.record_evidence(course_id="course_x", kind="随便编的类型", concept_id=concept_id_for("course_x", "链式法则"))


def test_projection_can_be_rebuilt_from_the_event_stream(learning):
    concept = concept_id_for("course_x", "链式法则")
    for kind in ("attempt_incorrect", "attempt_correct", "attempt_correct"):
        learning.record_evidence(course_id="course_x", kind=kind, concept_id=concept)
    before = learning.mastery(course_id="course_x")[0]

    assert learning.rebuild(course_id="course_x") == 1
    after = learning.mastery(course_id="course_x")[0]
    assert (after.score, after.objective_events) == (before.score, before.objective_events)


def test_concept_extraction_is_stable_and_skips_running_headers():
    pages = []
    for page in range(1, 11):
        body = "## 7 SCHEDULING: INTRODUCTION\n\n本页正文。\n"  # 每页都有的页眉
        if page in (3, 4):
            body += "\n### Round Robin\n\nRR 每个作业只运行一个时间片。\n"
        pages.append((page, body))

    names = [item["name"] for item in extract_candidates(pages)]
    assert "Round Robin" in names
    # 每页都出现的页眉不是概念。
    assert not any("SCHEDULING" in name for name in names)
    assert names == [item["name"] for item in extract_candidates(pages)]


def test_memory_patch_replaces_managed_block_and_keeps_handwritten_text(tmp_path):
    from modules.memory.store import MemoryStore

    store = MemoryStore(tmp_path)
    store.patch(scope="user", section="preferences", content="喜欢先给结论。")
    store.patch(scope="course", section="progress", content="学到第 7 章。", course_id="course_x")

    # 用户手写的段落必须活过 Agent 的下一次写入。
    path = tmp_path / "user.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n## 我的笔记\n别动这段。\n", encoding="utf-8")
    store.patch(scope="user", section="preferences", content="改成喜欢详细推导。")
    text = store.read_user()
    assert "别动这段。" in text
    assert "改成喜欢详细推导。" in text and "喜欢先给结论。" not in text
    assert text.count("agent:managed:preferences") == 2  # 区块被替换而不是重复追加
    assert "学到第 7 章。" in store.read_course("course_x")
    assert store.read_course("other_course") == ""  # 课程记忆互不可见

    for bad in ({"scope": "user", "section": "Bad Name", "content": "x"},
                {"scope": "course", "section": "progress", "content": "x"},
                {"scope": "user", "section": "ok", "content": "   "}):
        with pytest.raises(ValueError):
            store.patch(**bad)
