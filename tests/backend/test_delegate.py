"""delegate：把一件成规模的调研派给子任务，它自己带只读工具跑几轮再交回成果。

四条不能破的性质：子任务花掉的额度算在父轮头上；子 agent 不许再派子 agent、
不许加载 skill（注册期就要报错）；派子任务的意图闸门比排计划那道更紧；
子任务跑久了父轮的 turn 不能被抢占。
"""
from __future__ import annotations

import json
import re
import time

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.knowledge import ResolvedKnowledgeScope
from contracts.llm import ChatDelta, ChatFinal, ChatToolCalls, ToolCallRequest
from core.settings import Settings
from modules.agent import tools as tools_module
from modules.agent.service import _has_delegate_intent
from modules.agent.tools import (
    DELEGATE,
    MAIN,
    SUBAGENT_CAPABILITIES,
    SUBAGENT_TOOLS,
    CitationRegistry,
    ToolExecutor,
    specs_for,
    validate_profiles,
)

SCOPE = ResolvedKnowledgeScope(turn_id="turn_now", course_id="c1", resolver_version="v1")
MATERIAL_TEXT = "FIFO 调度下长作业会拖住后面的短作业，这就是护航效应。SJF 优先跑最短的作业。"
# 命中 _DELEGATE_INTENT 的一句话，几处 e2e 都拿它当开场。
DEEP_ASK = "帮我深入研究一下这门课里几种调度算法的取舍"


# ---- 注册期断言 ----

def test_the_registry_is_consistent_with_delegate_in_it():
    assert validate_profiles() == []
    assert "delegate" in MAIN.tools and DELEGATE in MAIN.capabilities
    assert MAIN.per_tool_budget["delegate"] == 2


@pytest.mark.parametrize("forbidden", ["delegate", "use_skill"])
def test_a_subagent_that_could_recurse_fails_at_registration(monkeypatch, forbidden):
    """子 agent 拿到 delegate 就能继续往下派，拿到 use_skill 就能在子循环里再展开一层规程。
    这两件必须在启动时就炸，不能等运行期。"""
    monkeypatch.setattr(tools_module, "SUBAGENT_TOOLS", (*SUBAGENT_TOOLS, forbidden))
    problems = validate_profiles()
    assert any("递归" in item and forbidden in item for item in problems), problems


def test_a_subagent_capability_set_that_allows_delegating_fails_at_registration(monkeypatch):
    monkeypatch.setattr(tools_module, "SUBAGENT_CAPABILITIES", SUBAGENT_CAPABILITIES | {DELEGATE})
    assert any("delegate 能力" in item for item in validate_profiles())


def test_the_application_refuses_to_start_on_a_recursive_subagent_profile(monkeypatch, tmp_path):
    """注册期校验真的接在启动路径上——单测绿而线上不设防是这道门存在的理由。"""
    from app.bootstrap import build_application

    # 用 use_skill 而不是 delegate：它的能力（free）本来就在子 agent 的声明里，
    # 所以只有「不许递归」这一条能拦住它——换成 delegate 的话能力校验也会报，
    # 这条测试就分不清是哪道门在起作用。
    monkeypatch.setattr(tools_module, "SUBAGENT_TOOLS", (*SUBAGENT_TOOLS, "use_skill"))
    with pytest.raises(RuntimeError, match="工具 profile 配置有问题"):
        build_application(_settings(tmp_path))


def test_the_subagent_only_gets_read_only_tools():
    """子任务没有界面，反问不了用户；写记忆、写计划、写产物都留给父轮。"""
    granted = {spec.name for spec in specs_for(SUBAGENT_TOOLS, capabilities=SUBAGENT_CAPABILITIES)}
    assert {"search_materials", "wiki_read", "web_search", "web_fetch"} <= granted
    for name in ("delegate", "use_skill", "ask_user", "memory_patch", "plan_update",
                 "artifact_append", "artifact_read", "note_write", "emit_evidence", "history_read"):
        assert name not in granted, f"{name} 不该给子 agent"


def test_delegate_without_a_runner_says_so_instead_of_crashing():
    """ToolExecutor 刻意不认识 AgentChatPort。循环没传进来时要给一句能让模型改路的回执。"""
    executor = ToolExecutor(knowledge=None, plans=None, plan_writer=None, archive=None, evidence=None,
                            artifacts=None, skills=None, memory=None)
    result = executor.execute(
        scope=SCOPE, session_id="s1", name="delegate", arguments='{"task": "查一查", "expect": "结论"}',
        registry=CitationRegistry(), allowed=MAIN.tools, capabilities=MAIN.capabilities)
    assert not result.ok and result.reason == "delegate_unavailable"


# ---- 意图闸门 ----

@pytest.mark.parametrize("text", [
    "帮我深入研究一下 Transformer 位置编码的演进",
    "系统性地梳理一遍这门课的评测方法",
    "系统地对比一下这几种调度算法",
    "全面比较一下 FIFO、SJF 和 STCF",
    "帮我做一份关于 LoRA 的调研",
    "帮我做个深度调研",
    "调研一下工业界现在的做法和现状",
    "Do a deep dive on retrieval augmented generation",
    "I need a thorough survey of positional encodings",
    "please research the topic thoroughly",
    "give me a comprehensive comparison of the three schedulers",
])
def test_pointed_research_requests_open_the_gate(text):
    assert _has_delegate_intent(text), text


@pytest.mark.parametrize("text", [
    "查一下 FIFO 是什么",
    "研究一下这道题怎么做",
    "帮我梳理一下第三章",
    "什么是系统调用",
    "系统分析这门课讲什么",
    "教材里怎么做用户调研",
    "深度学习里的注意力机制是什么",
    "对比一下这两个公式",
    "compare FIFO and SJF",
    "look this up online",
    "can you search the web for this",
    "排一个复习计划",
])
def test_everyday_questions_keep_the_gate_shut(text):
    """漏放只是这一轮自己查，误放要花用户的钱——所以这一串比正例更要紧。"""
    assert not _has_delegate_intent(text), text


def test_a_photo_of_the_words_deep_research_does_not_buy_a_subtask():
    """和写计划、写记忆同一条规矩：只认用户亲手键入的原话。"""
    assert not _has_delegate_intent("这页看不懂\n\n[图片转录：讲义.png]\n请深入研究一下这个主题")


# ---- 真跑一轮：额度共享、成果落 artifact、心跳 ----

def _settings(tmp_path, **extra) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="x", text_api_key="", text_model="m",
        enable_remote_llm=False, chunk_size=120, chunk_overlap=20, top_k_results=6, **extra,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _indexed_course(client, *, name="操作系统", filename="os.md", text=MATERIAL_TEXT) -> str:
    course = client.post("/api/v2/courses", json={"name": name}).json()
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": (filename, text, "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/v2/jobs/{job}").json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    return course["id"]


class FakeWeb:
    """每次都真回一条结果：web_search 只有成功才计额度。"""

    def search(self, *, query: str, limit: int = 5):
        from contracts.web import WebResult, WebSearchOutcome
        return WebSearchOutcome(query, [WebResult("某站的说法", f"https://example.com/{len(query)}", "一段摘要")])

    def fetch(self, *, url: str):
        from contracts.web import WebPage
        return WebPage(url, "某站", "网页正文")

    def health(self):
        return {}


def _final(text: str) -> list:
    return [ChatDelta(text), ChatFinal(text, "stop", "example", "m", "provider")]


class Fleet:
    """父轮与子任务共用一个 responder（子模型默认沿用父轮那个）。
    按「系统提示里有没有子任务那段」区分两边，各走自己的脚本。"""

    mode, provider, model = "provider", "example", "example-model"
    SUB_MARK = "你是一个子任务执行者"

    def __init__(self, parent, sub):
        self._parent, self._sub = list(parent), list(sub)
        self.parent_calls: list[list] = []
        self.sub_calls: list[list] = []

    def chat(self, *, messages, tools=()):
        is_sub = any(self.SUB_MARK in item.content for item in messages if item.role == "system")
        script = self._sub if is_sub else self._parent
        (self.sub_calls if is_sub else self.parent_calls).append([spec.name for spec in tools])
        yield from script.pop(0) if script else _final("好的。")

    def health(self):
        return {}

    def close(self):
        return None


def _delegating_parent(task="查清这几种调度算法的取舍", reply="综合下来 SJF 更短，但会饿死长作业。"):
    args = json.dumps({"task": task, "expect": "对比结论加出处"}, ensure_ascii=False)
    return [[ChatToolCalls((ToolCallRequest("d1", "delegate", args),))], _final(reply)]


def _searching_sub(queries: list[str], reply="子任务查到：FIFO 有护航效应。"):
    rounds = [[ChatToolCalls((ToolCallRequest(f"s{index}", "search_materials",
                                              json.dumps({"query": query}, ensure_ascii=False)),))]
              for index, query in enumerate(queries)]
    return [*rounds, _final(reply)]


def _run(client, session_id, fleet, *, request_id, message=DEEP_ASK) -> str:
    workspace(client).turns._responder = fleet
    return client.post(f"/api/v2/sessions/{session_id}/turns",
                       json={"client_request_id": request_id, "message": message}).text


def _session(client) -> tuple[str, str]:
    course_id = _indexed_course(client)
    return course_id, client.post("/api/v2/sessions",
                                  json={"scope_mode": "course", "course_id": course_id}).json()["id"]


def _tool_results(body: str) -> list[dict]:
    return [json.loads(frame.splitlines()[1].removeprefix("data: "))
            for frame in body.split("\n\n") if frame.startswith("event: tool_result")]


def test_the_subtask_runs_its_own_loop_and_hands_back_only_a_summary(client):
    _course_id, session_id = _session(client)
    fleet = Fleet(_delegating_parent(), _searching_sub(["护航效应", "SJF 饥饿"]))

    body = _run(client, session_id, fleet, request_id="d-1")

    results = [item for item in _tool_results(body) if item["name"] == "delegate"]
    assert results and results[0]["ok"], f"delegate 没跑通：{_tool_results(body)}"
    assert results[0]["summary_key"] == "summary.delegate_done"
    assert results[0]["summary_args"] == {"n": 2}
    # 子任务真的自己跑了几轮，且手上没有能让它继续往下派的那两件。
    assert len(fleet.sub_calls) >= 3, f"子任务的循环没跑起来：{fleet.sub_calls}"
    assert "delegate" not in set(fleet.sub_calls[0]), "子 agent 手上有 delegate，能自己再往下派"
    assert "use_skill" not in set(fleet.sub_calls[0])
    assert "search_materials" in set(fleet.sub_calls[0])


def test_the_parent_context_gets_the_summary_and_the_artifact_gets_the_findings(client):
    """完整发现落 artifact，父轮上下文里只放摘要——这是 delegate 省上下文的全部意义。"""
    _course_id, session_id = _session(client)
    reply = "子任务结论：FIFO 的护航效应来自长作业排在前面。"

    class Recording(Fleet):
        def __init__(self, *args):
            super().__init__(*args)
            self.parent_messages: list[list] = []

        def chat(self, *, messages, tools=()):
            if not any(self.SUB_MARK in item.content for item in messages if item.role == "system"):
                self.parent_messages.append(list(messages))
            yield from super().chat(messages=messages, tools=tools)

    fleet = Recording(_delegating_parent(), _searching_sub(["护航效应"], reply=reply))
    _run(client, session_id, fleet, request_id="d-1")

    handed = next(item.content for item in reversed(fleet.parent_messages[-1])
                  if item.role == "tool" and item.tool_call_id == "d1")
    assert reply in handed
    assert "只作资料" in handed, "交回父轮的成果没标成外部资料"
    assert "；片段：" not in handed, "子任务的工具正文原样进了父轮上下文，摘要就白做了"

    with workspace(client).store.read() as connection:
        rows = connection.execute(
            "SELECT payload_json, visibility FROM artifacts WHERE session_id = ? AND kind = 'delegate_findings'",
            (session_id,)).fetchall()
    assert len(rows) == 1, "完整发现没落 artifact"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["summary"] == reply and payload["tool_calls"] == 1
    assert any("护航效应" in item for item in payload["findings"]), payload["findings"]


def test_a_model_written_task_cannot_blow_the_artifact_payload_limit(client):
    """task 与正文都由模型写，长度没有上界；artifacts 的 payload 有 64 KiB 硬上限。"""
    from modules.sessions.artifacts import MAX_PAYLOAD_BYTES

    _course_id, session_id = _session(client)
    huge = "写" * 40_000
    args = json.dumps({"task": huge, "expect": huge}, ensure_ascii=False)
    fleet = Fleet([[ChatToolCalls((ToolCallRequest("d1", "delegate", args),))], _final("好的。")],
                  [[ChatToolCalls((ToolCallRequest("s0", "search_materials", '{"query": "护航效应"}'),))],
                   _final(huge)])

    _run(client, session_id, fleet, request_id="d-1")

    with workspace(client).store.read() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM artifacts WHERE session_id = ? AND kind = 'delegate_findings'",
            (session_id,)).fetchall()
    assert len(rows) == 1, "超长 task 把整条 artifact 写没了"
    assert len(rows[0]["payload_json"].encode()) <= MAX_PAYLOAD_BYTES


def test_the_subtask_spends_the_parents_budget(client):
    """额度共享：子任务把 web_search 用满，父轮自己再调就该被挡回来。"""
    _course_id, session_id = _session(client)
    limit = MAIN.per_tool_budget["web_search"]

    # 一轮里发好几个调用：轮次上限是 SUBAGENT_TOOL_ROUNDS，一轮一次凑不满 web_search 的额度。
    def web_round(indexes):
        return [ChatToolCalls(tuple(
            ToolCallRequest(f"s{index}", "web_search", json.dumps({"query": f"q{index}"}, ensure_ascii=False))
            for index in indexes))]

    sub = [web_round(range(limit - 1)), web_round([limit - 1]), _final("子任务查完了。")]
    args = json.dumps({"task": "查现状", "expect": "结论"}, ensure_ascii=False)
    parent = [
        [ChatToolCalls((ToolCallRequest("d1", "delegate", args),))],
        [ChatToolCalls((ToolCallRequest("w9", "web_search", '{"query": "父轮自己再查一次"}'),))],
        _final("说完了。"),
    ]
    fleet = Fleet(parent, sub)
    # 子任务要用到 web_search：联网工具得在册，适配器也得真回结果——
    # 回 not_configured 的话调用不计额度，这条测试就压不到额度共享。
    application = workspace(client)
    application.turns._offline = frozenset()
    application.turns._executor._web = FakeWeb()

    body = _run(client, session_id, fleet, request_id="d-1")

    parent_web = [item for item in _tool_results(body) if item["name"] == "web_search"]
    assert parent_web, f"父轮那次 web_search 没发生：{_tool_results(body)}"
    assert not parent_web[-1]["ok"], "子任务用掉的额度没算在父轮头上"
    assert parent_web[-1]["summary_key"] == "summary.budget_exhausted"


def test_a_long_subtask_does_not_let_the_parent_turn_be_taken_over(client):
    """子任务跑的时候父轮一个 SSE 事件都不发。没有心跳，60 秒后这一轮就被判失活、
    被下一轮抢占，回答写不回去。"""
    _course_id, session_id = _session(client)
    application = workspace(client)
    application.turns.HEARTBEAT_SECONDS = 0  # 每次都真写库，否则节流会把这条测试变成空过
    touched: list[str] = []
    original = application.sessions.touch_turn

    def spy(turn_id: str) -> bool:
        touched.append("beat")
        return original(turn_id)

    application.sessions.touch_turn = spy
    marks: list[str] = []

    class Marking(Fleet):
        def chat(self, *, messages, tools=()):
            is_sub = any(self.SUB_MARK in item.content for item in messages if item.role == "system")
            marks.append("sub" if is_sub else "parent")
            if is_sub:
                touched.append("sub-round")
            yield from super().chat(messages=messages, tools=tools)

    fleet = Marking(_delegating_parent(), _searching_sub(["a", "b", "c"]))
    _run(client, session_id, fleet, request_id="d-1")

    assert marks.count("sub") >= 4, f"子任务没跑够几轮，这条测试压不到心跳：{marks}"
    first, last = touched.index("sub-round"), len(touched) - 1 - touched[::-1].index("sub-round")
    assert "beat" in touched[first:last], f"子任务执行期间一次心跳都没有：{touched}"


def test_delegate_is_not_offered_unless_the_user_asked_for_a_real_investigation(client):
    """摘在工具集这一层：没有意图时模型连它的定义都看不到，不是调了再被拒。"""
    _course_id, session_id = _session(client)

    fleet = Fleet([_final("护航效应就是长作业挡路。")], [])
    _run(client, session_id, fleet, request_id="p-1", message="FIFO 为什么有护航效应")
    assert "delegate" not in set(fleet.parent_calls[0]), "没有意图也把 delegate 下发了"

    fleet = Fleet([_final("好的。")], [])
    _run(client, session_id, fleet, request_id="p-2", message=DEEP_ASK)
    assert "delegate" in set(fleet.parent_calls[0]), "明说要做调研却没下发 delegate"


def test_the_prompt_mentions_delegate_only_when_it_is_actually_on_offer():
    """单靠工具描述不够：实测提示词里没有这一句时，模型宁可自己连查三轮也不派。
    反过来，工具不在册还在提示词里推荐，它会口头答应去派而实际派不出去。"""
    from modules.agent.context import assemble_messages

    def system(**extra) -> str:
        return assemble_messages(
            course_name="操作系统", materials=["os.md"], history=[], question="帮我深入研究一下调度",
            seed_query="调度", seed_result_text="", history_token_budget=1000, **extra,
        ).messages[0].content

    assert "delegate" not in system()
    assert "4.1" in system(delegate_available=True) and "delegate" in system(delegate_available=True)


def test_a_delegating_turn_ships_the_hint_and_a_plain_turn_does_not(client):
    """闸门开关要同时管住工具下发和提示词那一句，不许各撤各的。"""
    _course_id, session_id = _session(client)

    class Peeking(Fleet):
        def __init__(self, *args):
            super().__init__(*args)
            self.systems: list[str] = []

        def chat(self, *, messages, tools=()):
            if not any(self.SUB_MARK in item.content for item in messages if item.role == "system"):
                self.systems.append(next(item.content for item in messages if item.role == "system"))
            yield from super().chat(messages=messages, tools=tools)

    plain = Peeking([_final("护航效应就是长作业挡路。")], [])
    _run(client, session_id, plain, request_id="p-1", message="FIFO 为什么有护航效应")
    assert "delegate" not in plain.systems[0]

    deep = Peeking([_final("好的。")], [])
    _run(client, session_id, deep, request_id="p-2", message=DEEP_ASK)
    assert "delegate" in deep.systems[0]


def test_calling_delegate_without_the_gate_is_refused_at_runtime(client):
    """schema 下发与运行期准入读同一份名单：模型硬调也拿不到。"""
    _course_id, session_id = _session(client)
    args = json.dumps({"task": "查现状", "expect": "结论"}, ensure_ascii=False)
    fleet = Fleet([[ChatToolCalls((ToolCallRequest("d1", "delegate", args),))], _final("那我自己查。")],
                  [_final("不该跑到这里")])

    body = _run(client, session_id, fleet, request_id="p-1", message="FIFO 为什么有护航效应")

    denied = [item for item in _tool_results(body) if item["name"] == "delegate"]
    assert denied and not denied[0]["ok"] and denied[0]["summary_key"] == "summary.not_in_profile"
    assert not fleet.sub_calls, "闸门关着却把子任务跑起来了"


def test_the_last_subtask_round_is_told_the_tools_are_gone(client):
    """真模型上栽过：最后一轮只是不下发 tools，子 agent 并不知道，接着写「让我再查一下」
    然后停住——那段过场话就成了交回父轮的成果。必须明说这是最后一次机会。"""
    from modules.agent.service import SUBAGENT_TOOL_ROUNDS

    _course_id, session_id = _session(client)

    class Peeking(Fleet):
        def __init__(self, *args):
            super().__init__(*args)
            self.sub_messages: list[list] = []

        def chat(self, *, messages, tools=()):
            if any(self.SUB_MARK in item.content for item in messages if item.role == "system"):
                self.sub_messages.append(list(messages))
            yield from super().chat(messages=messages, tools=tools)

    busy = [[ChatToolCalls((ToolCallRequest(f"s{index}", "search_materials", '{"query": "x"}'),))]
            for index in range(SUBAGENT_TOOL_ROUNDS)] + [_final("成果：护航效应来自长作业排在前面。")]
    fleet = Peeking(_delegating_parent(), busy)

    _run(client, session_id, fleet, request_id="d-1")

    early, final = fleet.sub_messages[0], fleet.sub_messages[-1]
    assert not any("工具调用次数已用完" in item.content for item in early), "还有轮次就催收尾"
    assert any("工具调用次数已用完" in item.content for item in final), "最后一轮没告诉子任务工具没了"
    assert fleet.sub_calls[-1] == []


def test_the_handoff_does_not_dangle_an_artifact_id_the_parent_cannot_fetch(client):
    """真模型上栽过：把 artifact id 摆进回执，父轮拿它去调 note_read，
    而 MAIN profile 里根本没有读产物的工具，白花一轮。"""
    _course_id, session_id = _session(client)

    class Recording(Fleet):
        def __init__(self, *args):
            super().__init__(*args)
            self.parent_messages: list[list] = []

        def chat(self, *, messages, tools=()):
            if not any(self.SUB_MARK in item.content for item in messages if item.role == "system"):
                self.parent_messages.append(list(messages))
            yield from super().chat(messages=messages, tools=tools)

    fleet = Recording(_delegating_parent(), _searching_sub(["护航效应"]))
    _run(client, session_id, fleet, request_id="d-1")

    handed = next(item.content for item in reversed(fleet.parent_messages[-1])
                  if item.role == "tool" and item.tool_call_id == "d1")
    assert "artifact_" not in handed, f"回执里摆了取不回来的 artifact id：{handed[-260:]}"
    assert "取不回来也不必再取" in handed


def test_a_subtask_that_never_stops_calling_tools_ends_with_no_findings(client):
    """轮次上限之后不再下发工具。子 agent 仍然只发调用就是没成果，父轮要拿到一句
    「自己查」而不是空字符串——循环必须终止。"""
    from modules.agent.service import SUBAGENT_TOOL_ROUNDS

    _course_id, session_id = _session(client)
    forever = [[ChatToolCalls((ToolCallRequest(f"s{index}", "search_materials", '{"query": "x"}'),))]
               for index in range(SUBAGENT_TOOL_ROUNDS + 2)]
    fleet = Fleet(_delegating_parent(), forever)

    body = _run(client, session_id, fleet, request_id="d-1")

    results = [item for item in _tool_results(body) if item["name"] == "delegate"]
    assert results and not results[0]["ok"] and results[0]["summary_key"] == "summary.delegate_empty"
    assert len(fleet.sub_calls) == SUBAGENT_TOOL_ROUNDS + 1, f"轮次上限没生效：{len(fleet.sub_calls)}"
    assert fleet.sub_calls[-1] == [], "最后一轮还在下发工具，模型就没有收尾的机会"


def test_a_subtask_with_no_task_is_rejected_before_any_model_call(client):
    _course_id, session_id = _session(client)
    fleet = Fleet([[ChatToolCalls((ToolCallRequest("d1", "delegate", '{"expect": "结论"}'),))],
                   _final("我自己查。")], [_final("不该跑到这里")])

    body = _run(client, session_id, fleet, request_id="d-1")

    results = [item for item in _tool_results(body) if item["name"] == "delegate"]
    assert results and not results[0]["ok"] and results[0]["summary_key"] == "summary.delegate_no_task"
    assert not fleet.sub_calls


def test_the_subtasks_tool_bodies_land_in_messages_under_their_own_call_ids(client):
    """子任务查到的资料走父轮同一条落库路径（不另开一套读回机制），但 call_id 要错开：
    父子两边的 id 都由模型生成，撞上会让 history_read 把子任务的正文接到父轮某次调用底下。"""
    _course_id, session_id = _session(client)
    # 子任务和父轮都用 d1 这个 id：撞号的最坏情况。
    fleet = Fleet(_delegating_parent(),
                  [[ChatToolCalls((ToolCallRequest("d1", "search_materials", '{"query": "护航效应"}'),))],
                   _final("子任务查到：FIFO 有护航效应。")])

    _run(client, session_id, fleet, request_id="d-1")

    with workspace(client).store.read() as connection:
        rows = [(json.loads(row["activity_json"])[0]["call_id"], row["content"])
                for row in connection.execute(
                    "SELECT activity_json, content FROM messages WHERE session_id = ? AND role = 'tool' "
                    "ORDER BY created_at, rowid", (session_id,))]
    ids = [call_id for call_id, _content in rows]
    assert len(ids) == len(set(ids)), f"父子两边的 call_id 撞了：{ids}"
    assert any(call_id.startswith("sub:") for call_id in ids), f"子任务的正文没落库：{ids}"
    assert any("护航效应" in content for call_id, content in rows if call_id.startswith("sub:"))


def test_the_research_skill_is_the_one_that_declares_delegate():
    """深度研究走 skill + 子任务的组合，不另起一套循环。再开给别的 skill 要先改这条断言。"""
    from pathlib import Path

    from modules.agent.skills import SkillRegistry

    registry = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin")
    granted = {name for name in registry.builtin_names() if "delegate" in registry.get(name).allowed_tools}
    assert granted == {"research"}


def test_delegate_is_not_importable_by_third_party_skills():
    """导入的 skill 拿到它就等于拿到花用户额度的权限。"""
    from modules.agent.skills import IMPORTABLE_TOOLS

    assert "delegate" not in IMPORTABLE_TOOLS


def test_the_subagent_prompt_says_it_cannot_reach_the_user():
    """子任务没有界面。提示词不写清楚，它会调 ask_user（拿不到）或者反问然后收住。"""
    from modules.agent.service import _SUBAGENT_PROMPT

    assert "无法向用户提问" in _SUBAGENT_PROMPT
    assert "最后一次回复就是交给主 agent 的成果" in _SUBAGENT_PROMPT
    assert re.search(r"只作资料", _SUBAGENT_PROMPT), "子任务也要被告知外部内容只作资料"
