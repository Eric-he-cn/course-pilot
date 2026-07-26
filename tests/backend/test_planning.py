from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.store import SQLiteStore
from modules.planning.api import PlanConflictError
from modules.planning.repository import PlanningRepository
from modules.planning.service import PlanningService

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


def _service(tmp_path, *, known: set[str] | None = None) -> tuple[PlanningService, SQLiteStore]:
    store = SQLiteStore(tmp_path / "plan.db")
    store.migrate()
    with store.write() as connection:
        connection.execute(
            "INSERT INTO courses(id, name, color, wiki_enabled, created_at, updated_at) VALUES ('course_1','算法','#059669',0,'now','now')"
        )
    concepts = known or set()
    return PlanningService(PlanningRepository(store), concept_exists=lambda course_id, concept_id: concept_id in concepts), store


def test_first_write_creates_plan_and_records_revision(tmp_path):
    service, store = _service(tmp_path, known={"concept_a"})
    diff = service.update_plan(
        course_id="course_1", expected_version=0, note="期末备考",
        items=[{"due_date": TOMORROW, "title": "复习分治", "concept_id": "concept_a"}], turn_id="turn_1",
    )
    assert (diff.version_from, diff.version_to, diff.added) == (0, 1, 1)
    plan = service.get_plan(course_id="course_1")
    assert plan is not None and plan.version == 1
    assert plan.items[0].title == "复习分治"
    with store.read() as connection:
        revision = connection.execute("SELECT * FROM plan_revisions").fetchone()
    assert revision["version"] == 1 and revision["turn_id"] == "turn_1" and revision["note"] == "期末备考"


def test_stale_expected_version_is_rejected(tmp_path):
    service, _ = _service(tmp_path)
    service.update_plan(course_id="course_1", expected_version=0, items=[{"due_date": TOMORROW, "title": "第一版"}])
    with pytest.raises(PlanConflictError) as error:
        service.update_plan(course_id="course_1", expected_version=0, items=[{"due_date": TOMORROW, "title": "第二版"}])
    assert error.value.current_version == 1


def test_past_items_survive_a_rewrite(tmp_path):
    """历史条目不可修改：重排只重写今天及以后的待办。"""
    service, store = _service(tmp_path)
    service.update_plan(course_id="course_1", expected_version=0, items=[{"due_date": TOMORROW, "title": "旧安排"}])
    plan = service.get_plan(course_id="course_1")
    assert plan is not None
    with store.write() as connection:
        connection.execute(
            "INSERT INTO plan_items(id, plan_id, due_date, title, concept_id, status, created_at)"
            " VALUES ('item_past', ?, ?, '上周做过的', NULL, 'done', 'now')", (plan.id, YESTERDAY),
        )
    diff = service.update_plan(course_id="course_1", expected_version=1, items=[{"due_date": TOMORROW, "title": "新安排"}])
    assert (diff.removed, diff.kept_past) == (1, 1)
    titles = {item.title for item in service.get_plan(course_id="course_1").items}
    assert titles == {"上周做过的", "新安排"}


def test_started_future_items_are_not_reset(tmp_path):
    """已开始的未来条目重写会让状态回退、id 变化，所以保留不动。"""
    service, store = _service(tmp_path)
    service.update_plan(course_id="course_1", expected_version=0, items=[{"due_date": TOMORROW, "title": "已做完的"}])
    plan = service.get_plan(course_id="course_1")
    with store.write() as connection:
        connection.execute("UPDATE plan_items SET status = 'done' WHERE plan_id = ?", (plan.id,))
    diff = service.update_plan(course_id="course_1", expected_version=1, items=[{"due_date": TOMORROW, "title": "新加的"}])
    assert (diff.removed, diff.kept_locked) == (0, 1)
    items = service.get_plan(course_id="course_1").items
    assert {item.title for item in items} == {"已做完的", "新加的"}
    assert next(item.status for item in items if item.title == "已做完的") == "done"


@pytest.mark.parametrize(
    "items, expected",
    [
        ([], "至少要有一个条目"),
        ([{"due_date": "2026/07/30", "title": "格式不对"}], "不是 YYYY-MM-DD"),
        ([{"due_date": YESTERDAY, "title": "改历史"}], "历史条目不可修改"),
        ([{"due_date": TOMORROW, "title": "  "}], "缺少 title"),
        ([{"due_date": TOMORROW, "title": "挂了个不存在的概念", "concept_id": "concept_ghost"}], "不在本课程概念目录"),
    ],
)
def test_invalid_items_reject_the_whole_batch(tmp_path, items, expected):
    service, _ = _service(tmp_path)
    with pytest.raises(ValueError) as error:
        service.update_plan(course_id="course_1", expected_version=0, items=items)
    assert expected in str(error.value)
    assert service.get_plan(course_id="course_1") is None  # 整批拒绝，不留半个计划
