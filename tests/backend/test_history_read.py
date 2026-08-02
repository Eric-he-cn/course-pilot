"""history_read：把被读时投影裁掉的历史工具痕迹与引用原文读回来。

跨轮历史只送 user/assistant 的正文（context._budgeted_history 按 role 过滤），
当时检索到的教材片段、网页内容与工具结果都不进上下文。没有这个工具，
「读时投影」和直接裁掉没有区别。
"""
from __future__ import annotations

import pytest

from contracts.knowledge import ResolvedKnowledgeScope
from modules.agent.tools import (
    HISTORY_MAX_CHARS,
    HISTORY_MAX_TURNS,
    MAIN,
    MAIN_PROFILE,
    TOOL_CAPABILITY,
    CitationRegistry,
    ToolExecutor,
)
from modules.sessions.models import Message

SCOPE = ResolvedKnowledgeScope(turn_id="turn_now", course_id="c1", resolver_version="v1")


def _message(
    *, turn_id: str, role: str, content: str = "", created_at: str = "2026-07-30T01:00:00Z",
    citations: list[dict] | None = None, activity: list[dict] | None = None, course_id: str = "c1",
) -> Message:
    return Message(
        f"message_{turn_id}_{role}", turn_id, role, content, citations or [], "complete", created_at,
        resolution_status="resolved", resolved_course_id=course_id,
        activity=activity or [],
    )


class FakeSessions:
    """只实现 MessageHistoryPort 需要的那一个动作。"""

    def __init__(self, messages: list[Message]) -> None:
        self._messages = messages

    def list_messages(self, session_id: str) -> list[Message]:
        return list(self._messages)


def _executor(messages: list[Message]) -> ToolExecutor:
    return ToolExecutor(
        knowledge=None, plans=None, plan_writer=None, archive=None, evidence=None,
        artifacts=None, skills=None, memory=None, sessions=FakeSessions(messages),
    )


def _read(messages: list[Message], arguments: str = "{}"):
    return _executor(messages).execute(
        scope=SCOPE, session_id="s1", name="history_read", arguments=arguments,
        registry=CitationRegistry(), allowed=MAIN_PROFILE, capabilities=MAIN.capabilities,
        budget=MAIN.per_tool_budget,
    )


def _turn(
    index: int, *, citations: list[dict] | None = None, activity: list[dict] | None = None, course_id: str = "c1",
) -> list[Message]:
    stamp = f"2026-07-30T0{index}:00:00Z"
    return [
        _message(turn_id=f"turn_{index}", role="user", content=f"第 {index} 轮的问题",
                 created_at=stamp, course_id=course_id),
        _message(turn_id=f"turn_{index}", role="assistant", content=f"第 {index} 轮的回答",
                 created_at=stamp, citations=citations, activity=activity, course_id=course_id),
    ]


MATERIAL = {
    "kind": "material", "number": 1, "document": "操作系统原理.pdf", "page": 42,
    "chunk_id": "chunk-42", "snippet": "FIFO 调度下一个长作业会把后面的短作业全部拖住，这就是护航效应。",
}
ACTIVITY = [
    {"call_id": "c1", "name": "search_materials", "origin": "seed", "ok": True, "summary": "检索「护航效应」命中 3 段"},
    {"call_id": "c2", "name": "web_fetch", "origin": "model", "ok": False, "summary": "联网失败"},
]


def test_reads_back_the_previous_turn_tool_summaries_and_citation_snippets():
    """默认 turns=1 就该拿到上一轮的工具摘要与引用原文——这是这个工具存在的理由。"""
    result = _read(_turn(1, citations=[MATERIAL], activity=ACTIVITY))

    assert result.ok
    assert "search_materials" in result.text and "检索「护航效应」命中 3 段" in result.text
    assert "web_fetch" in result.text and "（失败）" in result.text
    assert "操作系统原理.pdf" in result.text and "第 42 页" in result.text
    assert "护航效应" in result.text  # snippet 原文，重查未必再命中同一个 chunk
    assert result.summary_key == "summary.history_read" and result.summary_args == {"n": 1}


def test_old_citation_numbers_are_not_handed_back():
    """当时的 [1] 在本轮指的是另一个片段。把旧编号摆回去等于诱导误标，
    所以只回放来源与原文，编号一律由本轮的检索工具产生。"""
    result = _read(_turn(1, citations=[MATERIAL]))
    assert "[1]" not in result.text
    assert "search_materials" in result.text  # 提示词里写明了要重查才能拿本轮编号


def test_current_turn_is_excluded():
    """当前轮的用户消息在 history_read 执行前就已落库，回放它是纯浪费。"""
    messages = _turn(1, citations=[MATERIAL]) + [
        _message(turn_id="turn_now", role="user", content="那你刚才查到的原文是什么"),
    ]
    result = _read(messages, '{"turns": 5}')
    assert "那你刚才查到的原文" not in result.text
    assert result.summary_args == {"n": 1}


def test_turns_cap_holds():
    """turns 上限挡住"把全部历史翻回来"。要 99 轮也只给 HISTORY_MAX_TURNS 轮。"""
    messages = [item for index in range(1, 9) for item in _turn(index, citations=[MATERIAL])]
    result = _read(messages, '{"turns": 99}')

    assert result.summary_args == {"n": HISTORY_MAX_TURNS}
    assert f"往前第 {HISTORY_MAX_TURNS} 轮" in result.text
    assert f"往前第 {HISTORY_MAX_TURNS + 1} 轮" not in result.text
    # 给的是最近的几轮，不是最早的几轮
    assert "第 8 轮的问题" in result.text and "第 1 轮的问题" not in result.text


def test_dropping_whole_turns_says_how_many_and_offers_a_smaller_turns():
    """丢掉整轮时才可以建议减小 turns，而且要说清丢了几轮。"""
    fat = [dict(MATERIAL, chunk_id=f"chunk-{n}", snippet="片" * 400) for n in range(4)]
    messages = [item for index in range(1, 6) for item in _turn(index, citations=fat)]
    result = _read(messages, '{"turns": 5}')

    assert len(result.text) <= HISTORY_MAX_CHARS, "单次返回超过上限，压缩机制就被这个工具废掉了"
    kept = result.summary_args["n"]
    assert 0 < kept < 5
    assert f"更早的 {5 - kept} 轮没有列出" in result.text
    assert "减小 turns" in result.text
    assert "中途被切断" not in result.text, "没有轮内截断就不该说有"


def test_a_single_oversized_turn_says_the_turn_itself_was_cut_off():
    """turns=1 时单轮自己就超限：这时没有更早的轮次被丢，"减小 turns"无从执行。
    说错了模型会把"只给到这里"当成"当时只查到这些"。"""
    fat = [dict(MATERIAL, chunk_id=f"chunk-{n}", snippet="片" * 400) for n in range(40)]
    result = _read(_turn(1, citations=fat), '{"turns": 1}')

    assert len(result.text) <= HISTORY_MAX_CHARS
    assert "往前第 1 轮的记录在中途被切断，后面还有内容没给" in result.text
    assert "减小 turns" not in result.text, "turns 已经是 1，这条建议无从执行"
    assert "更早的" not in result.text, "并没有整轮被丢掉"
    assert "操作系统原理.pdf" in result.text  # 截着给也比整个调用白跑好


def test_both_kinds_of_truncation_are_reported_together():
    """最近这一轮自己就超限、后面还有别的轮次：两件事都要说。"""
    fat = [dict(MATERIAL, chunk_id=f"chunk-{n}", snippet="片" * 400) for n in range(40)]
    messages = [item for index in range(1, 4) for item in _turn(index, citations=fat)]
    result = _read(messages, '{"turns": 3}')

    assert "中途被切断" in result.text and "更早的 2 轮没有列出" in result.text
    assert "减小 turns" not in result.text  # 减到 1 也装不下第 1 轮


def test_the_note_reserve_covers_the_longest_note():
    """预留额度不够，最长的那句截断说明会把总长顶过上限。"""
    from modules.agent.tools import _HISTORY_NOTE_RESERVE, _history_limit_note

    longest = max(len(_history_limit_note(dropped, clipped))
                  for dropped in (0, HISTORY_MAX_TURNS - 1) for clipped in (False, True))
    assert longest <= _HISTORY_NOTE_RESERVE


@pytest.mark.parametrize(
    "kind, present, absent",
    [("tools", "search_materials", "操作系统原理.pdf"), ("citations", "操作系统原理.pdf", "检索「护航效应」")],
)
def test_kind_filters_to_one_class(kind, present, absent):
    result = _read(_turn(1, citations=[MATERIAL], activity=ACTIVITY), '{"kind": "%s"}' % kind)
    assert present in result.text and absent not in result.text


def test_unknown_kind_is_rejected():
    result = _read(_turn(1, citations=[MATERIAL]), '{"kind": "everything"}')
    assert not result.ok and result.reason == "invalid_args"


@pytest.mark.parametrize("turns", ['[1]', '{"a": 1}', '"abc"'])
def test_a_malformed_turns_reads_as_invalid_args(turns):
    """参数写坏了要告诉模型「参数无效」，它才知道改参数重试；归到执行失败还会白打一条
    ERROR 堆栈。空值（0、[]、{}）不算写坏，按没传处理。"""
    result = _read(_turn(1, citations=[MATERIAL]), f'{{"turns": {turns}}}')
    assert not result.ok and result.reason == "invalid_args"


def test_empty_history_answers_plainly():
    """新会话里调用它不该像出错。"""
    result = _read([_message(turn_id="turn_now", role="user", content="你好")])
    assert result.ok and result.summary_key == "summary.history_empty"
    assert "没有可回看的记录" in result.text


def test_turn_without_tools_or_citations_says_so():
    """纯聊天那一轮要明确说"没有记录"，不然模型会以为工具查过而自己没读到。"""
    result = _read(_turn(1))
    assert result.ok and "没有留下工具痕迹或引用" in result.text


def test_other_courses_turns_are_not_readable():
    """通用会话里相邻两轮可以落在不同课程上。不过滤就等于把隔壁课的教材原文
    当成本课程的依据端上来，而且不会被标成"不是当前教材结论"。"""
    other = {**MATERIAL, "document": "编译原理.pdf", "snippet": "LR(1) 项集族的构造从增广文法开始。"}
    messages = _turn(1, citations=[other], activity=ACTIVITY, course_id="c2") + _turn(2, citations=[MATERIAL])

    result = _read(messages, '{"turns": 5}')
    assert "编译原理.pdf" not in result.text and "LR(1)" not in result.text
    assert "操作系统原理.pdf" in result.text
    assert result.summary_args == {"n": 1}


def test_a_session_with_only_other_courses_turns_reads_as_empty():
    """一轮都不属于当前课程时要明确说没有记录，不能露出别课的存在。"""
    result = _read(_turn(1, citations=[MATERIAL], activity=ACTIVITY, course_id="c2"), '{"turns": 5}')
    assert result.ok and result.summary_key == "summary.history_empty"
    assert "操作系统原理.pdf" not in result.text and "search_materials" not in result.text


def test_model_private_artifacts_never_come_through_history():
    """artifacts 有 model_private 一档。history_read 只读消息记录，
    连产物表都不碰——多一条读取路径就多一个泄露口。"""
    import inspect

    from modules.agent.tools import ToolExecutor as Executor

    source = inspect.getsource(Executor._history_read)
    assert "_artifacts" not in source, "history_read 一旦读 artifacts，就得自己再判一次 visibility"

    # 库里真放一条私有产物：它的 payload 出现在回放里就是泄露。消息记录里只有
    # activity 那句 summary，产物正文从来不进 messages。
    class ArtifactsWithASecret:
        def recent(self, *, session_id, kind=None, limit=5):
            from modules.sessions.artifacts import Artifact
            return [Artifact("artifact_1", "c1", session_id, "practice", "model_private",
                             {"answer_key": "正确答案是 B，评分要点见下"}, "2026-07-30T01:00:00Z")]

    saved = [{"call_id": "c9", "name": "artifact_append", "ok": True, "summary": "存 practice"}]
    messages = _turn(1, activity=saved)
    executor = ToolExecutor(
        knowledge=None, plans=None, plan_writer=None, archive=None, evidence=None,
        artifacts=ArtifactsWithASecret(), skills=None, memory=None, sessions=FakeSessions(messages),
    )
    result = executor.execute(
        scope=SCOPE, session_id="s1", name="history_read", arguments="{}",
        registry=CitationRegistry(), allowed=MAIN_PROFILE, capabilities=MAIN.capabilities,
    )
    assert "存 practice" in result.text  # 工具痕迹照常回放
    assert "正确答案是 B" not in result.text and "model_private" not in result.text


TOOL_BODY = "[1] 文档：操作系统原理.pdf，第 42 页；片段：chunk-42\nFIFO 让长作业挡在前面，后面的短作业全被拖住。"


def _tool_row(index: int, *, call_id: str = "c1", name: str = "search_materials",
              body: str = TOOL_BODY, course_id: str = "c1") -> Message:
    return Message(
        f"message_turn_{index}_tool_{call_id}", f"turn_{index}", "tool", body, [], "complete",
        f"2026-07-30T0{index}:00:00Z", resolution_status="resolved", resolved_course_id=course_id,
        activity=[{"call_id": call_id, "name": name}],
    )


def test_the_persisted_tool_body_comes_back_under_its_summary_line():
    """摘要说明当时调了什么，正文接在它下面。摘要来自 activity、正文来自 role='tool' 的行，
    是同一张表的两半，不是两条读回路径。"""
    messages = _turn(1, activity=ACTIVITY) + [_tool_row(1)]
    result = _read(messages)

    assert result.ok
    head = "- 工具 search_materials：检索「护航效应」命中 3 段"
    assert head in result.text
    assert result.text.index(head) < result.text.index("片段：chunk-42"), "正文没有接在它自己那行摘要下面"
    assert "全被拖住" in result.text


def test_a_turn_with_tool_bodies_does_not_also_replay_the_citation_snippets():
    """正文里已经是那几段原文的全文，引用摘要是它的子集。两份都贴等于把额度花在重复内容上。"""
    result = _read(_turn(1, citations=[MATERIAL], activity=ACTIVITY) + [_tool_row(1)])

    assert "片段：chunk-42" in result.text
    assert "- 引用｜" not in result.text, "工具正文和引用摘要贴了两遍"


def test_turns_without_a_persisted_body_still_replay_their_citations():
    """落库这套上线之前的老轮次只有引用摘要。它们不能因为新机制而读不到。"""
    result = _read(_turn(1, citations=[MATERIAL], activity=ACTIVITY))
    assert "操作系统原理.pdf" in result.text and "护航效应" in result.text


def test_kind_citations_never_returns_a_tool_body():
    """kind 是给模型收窄用的。要引用原文时不该被大段工具正文顶掉额度。"""
    result = _read(_turn(1, citations=[MATERIAL], activity=ACTIVITY) + [_tool_row(1)], '{"kind": "citations"}')
    assert "操作系统原理.pdf" in result.text and "片段：chunk-42" not in result.text


def test_an_orphan_tool_body_is_still_readable():
    """那一轮中途失败、助手消息没落库时，工具正文仍在库里——读不到就等于白存。"""
    messages = [
        _message(turn_id="turn_1", role="user", content="第 1 轮的问题", created_at="2026-07-30T01:00:00Z"),
        _tool_row(1),
    ]
    result = _read(messages)
    assert "片段：chunk-42" in result.text and "没有留下工具痕迹或引用" not in result.text


def test_another_courses_tool_body_is_not_readable():
    """按课程过滤要盖住工具正文这条新路径，不然隔壁课的教材原文照样端得上来。"""
    other = _tool_row(1, body="[1] 文档：编译原理.pdf；片段：chunk-9\nLR(1) 项集族从增广文法开始。", course_id="c2")
    messages = _turn(1, activity=ACTIVITY, course_id="c2") + [other] + _turn(2, citations=[MATERIAL])

    result = _read(messages, '{"turns": 5}')
    assert "编译原理.pdf" not in result.text and "LR(1)" not in result.text


def test_a_fat_tool_body_still_respects_the_single_call_limit():
    """正文比引用摘要大得多。上限守不住，这个工具就把压缩机制废掉了。"""
    fat = [_tool_row(index, body="料" * 3000) for index in range(1, 4)]
    messages = [item for index in range(1, 4) for item in _turn(index, activity=ACTIVITY)] + fat
    result = _read(messages, '{"turns": 3}')

    assert len(result.text) <= HISTORY_MAX_CHARS
    assert "已到单次返回上限" in result.text


def test_history_read_is_a_read_only_course_capability_with_a_call_budget():
    """只读、不碰用户数据，与 get_archive / note_read 同档；次数有上限，
    免得模型把省下来的上下文又用翻历史填回去。"""
    assert TOOL_CAPABILITY["history_read"] == TOOL_CAPABILITY["note_read"] == "read_course"
    assert "history_read" in MAIN.tools
    assert MAIN.per_tool_budget["history_read"] == 3


def test_the_skills_that_need_history_declare_it():
    """profile 是整体替换：skill 一激活，没声明的工具就消失。practice 在
    「有练习待批改」的每一轮都会自动加载，而"你刚才引用的原文是什么"正好
    落在这条路径上——不声明的话工具在最常见的场景里反而不可用。
    再开给别的 skill 要先改这条断言，不让它悄悄扩散。"""
    from pathlib import Path

    from modules.agent.skills import SkillRegistry

    registry = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin")
    granted = {name for name in registry.builtin_names() if "history_read" in registry.get(name).allowed_tools}
    assert granted == {"practice", "research", "mistake_review"}


def test_history_read_is_not_importable_by_third_party_skills():
    """历史里可能有别的 skill 产生的内容，第一版不开放给导入的 skill。"""
    from modules.agent.skills import IMPORTABLE_TOOLS

    assert "history_read" not in IMPORTABLE_TOOLS


class Scripted:
    """按脚本逐次响应，并记下每次收到的 messages（工具回执正文只在那里）。"""

    mode, provider, model = "provider", "example", "example-model"

    def __init__(self, script):
        self._script, self.calls = list(script), []

    def chat(self, *, messages, tools=()):
        self.calls.append(list(messages))
        yield from self._script.pop(0)

    def health(self):
        return {}

    def close(self):
        return None


@pytest.fixture
def live(tmp_path):
    """真实 app + 真实 SQLite，模型换成脚本。"""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from core.settings import Settings

    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6,
    )
    with TestClient(create_app(settings=settings)) as client:
        yield client


def _indexed_course(client, *, name: str, filename: str, text: str) -> str:
    import time

    course = client.post("/api/v2/courses", json={"name": name}).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": (filename, text, "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/v2/jobs/{job}").json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    return course["id"]


def _answer(client, workspace, session_id, *, request_id: str, message: str, script) -> tuple[str, Scripted]:
    scripted = Scripted(script)
    workspace.turns._responder = scripted
    body = client.post(f"/api/v2/sessions/{session_id}/turns",
                       json={"client_request_id": request_id, "message": message}).text
    return body, scripted


def _tool_results(body: str) -> list[dict]:
    import json

    return [json.loads(frame.splitlines()[1].removeprefix("data: "))
            for frame in body.split("\n\n") if frame.startswith("event: tool_result")]


def _history_call(scripted: Scripted, call_id: str = "c1") -> str:
    """history_read 的回执正文只出现在发给模型的消息里，SSE 不外泄它。"""
    return next(item for item in reversed(scripted.calls[-1])
                if item.role == "tool" and item.tool_call_id == call_id).content


def _read_history_script(reply: str):
    from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest

    return [
        [ChatToolCalls((ToolCallRequest("c1", "history_read", '{"turns": 5}'),))],
        [ChatDelta(reply), ChatFinal(reply, "stop", "example", "m", "provider")],
    ]


def _plain_script(reply: str):
    from contracts.llm import ChatDelta, ChatFinal

    return [[ChatDelta(reply), ChatFinal(reply, "stop", "example", "m", "provider")]]


def test_wired_end_to_end_so_a_later_turn_can_read_the_earlier_evidence(live):
    """真跑两轮：第一轮检索留下引用，第二轮模型调 history_read，
    回执里要有当时的原文。这条守的是装配——ToolExecutor 拿不到会话端口时，
    单测全绿而线上只会回一句"读取未启用"。
    """
    from conftest import workspace

    course_id = _indexed_course(live, name="操作系统", filename="os.md",
                               text="FIFO 调度下长作业会拖住后面的短作业，这就是护航效应。")
    session_id = live.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course_id}).json()["id"]

    _answer(live, workspace(live), session_id, request_id="h-1",
            message="FIFO 为什么有护航效应", script=_plain_script("护航效应。[1]"))
    body, scripted = _answer(live, workspace(live), session_id, request_id="h-2",
                             message="你刚才引用的原文是什么", script=_read_history_script("就是上一轮那段。"))

    results = _tool_results(body)
    history = [item for item in results if item["name"] == "history_read"]
    assert history and history[0]["ok"], f"history_read 没跑通：{results}"
    assert history[0]["summary_key"] == "summary.history_read"

    text = _history_call(scripted)
    assert "护航效应" in text and "os.md" in text


def test_a_general_session_cannot_read_another_courses_evidence(live):
    """通用会话逐轮解析课程：第一轮落在编译原理，第二轮落在操作系统。
    第二轮回看历史读不到编译原理的教材原文——README 的「边界」写明了回看只回放当前课程，
    而跨课程的原文一旦端上来，还会因为看着像本课程证据而不被标注来源。
    """
    from conftest import workspace

    _indexed_course(live, name="编译原理", filename="compiler.md",
                    text="LR(1) 项集族的构造从增广文法 S' → S 开始。")
    _indexed_course(live, name="操作系统", filename="os.md",
                    text="FIFO 调度下长作业会拖住后面的短作业，这就是护航效应。")
    session_id = live.post("/api/v2/sessions", json={"scope_mode": "general", "course_id": None}).json()["id"]

    _answer(live, workspace(live), session_id, request_id="g-1",
            message="编译原理里 LR(1) 项集族怎么构造", script=_plain_script("从增广文法开始。[1]"))
    body, scripted = _answer(live, workspace(live), session_id, request_id="g-2",
                             message="操作系统里 FIFO 为什么有护航效应，你之前查到的原文是什么",
                             script=_read_history_script("这一轮查到的是护航效应那段。"))

    messages = live.get(f"/api/v2/sessions/{session_id}/messages").json()["messages"]
    resolved = [item["resolved_course_name"] for item in messages if item["role"] == "user"]
    assert resolved == ["编译原理", "操作系统"], f"前置条件不成立，两轮没落在不同课程上：{resolved}"

    history = [item for item in _tool_results(body) if item["name"] == "history_read"]
    assert history and history[0]["ok"]
    text = _history_call(scripted)
    assert "compiler.md" not in text and "增广文法" not in text and "LR(1)" not in text


def test_budget_exhaustion_is_reported_instead_of_reading_again():
    executor = _executor(_turn(1, citations=[MATERIAL]))
    used = {"history_read": MAIN.per_tool_budget["history_read"]}
    result = executor.execute(
        scope=SCOPE, session_id="s1", name="history_read", arguments="{}",
        registry=CitationRegistry(), allowed=MAIN_PROFILE, capabilities=MAIN.capabilities,
        budget=MAIN.per_tool_budget, used=used,
    )
    assert not result.ok and result.reason == "budget_exhausted"
