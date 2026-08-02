"""分区配额与总闸：文档里那张表要真的在拦，不是写着好看。

判据分两层：组装时逐段核对配额（超了只裁本段），以及工具循环每轮都过一次的总闸。
两层都要求「裁了就说出来」——静默截断读起来像「资料就这些」。
"""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from conftest import workspace
from fastapi.testclient import TestClient

from app.main import create_app
from contracts.llm import ChatDelta, ChatFinal, ChatMessage, ChatToolCalls, ToolCallRequest
from core.settings import CONTEXT_PARTITION_RATIOS, PartitionLimits, Settings
from modules.agent.context import (
    GATE_TOOL_NOTE, SEED_CALL_ID, assemble_general_messages, assemble_messages,
    enforce_context_limit, estimate_tokens, message_tokens, tool_schema_tokens,
)
from modules.agent.tools import MAIN, MAIN_PROFILE, WIKI_TOOLS, profile_for_skill, specs_for, without_tools

# 各段都远小于配额的一轮，用来证明限额不碰正常对话。
ORDINARY = dict(
    course_name="操作系统", materials=["os.pdf", "习题课.pdf"],
    history=[("user", "什么是护航效应？"), ("assistant", "短作业排在长作业后面会被拖慢。[1]")],
    question="FIFO 为什么会有护航效应？", seed_query="FIFO 护航效应",
    seed_result_text="[1] os.pdf p.10：先来先服务下，长作业会把后面的短作业堵住。",
    history_token_budget=128_000, memory="用户偏好：讲解要给例子。",
    conversation_summary="之前聊过进程与线程的区别。", today="2026-08-02",
)
PRODUCTION = PartitionLimits.from_window(512_000)


def _segments(assembled) -> dict[str, int]:
    return {item.key: item.tokens for item in assembled.segments}


def _clips(assembled) -> dict[str, tuple[int, int]]:
    return {item.key: (item.before, item.after) for item in assembled.clips}


def _untouched(flooded, ordinary, *, materials=True, question=True, evidence=True, history=True) -> None:
    """别的分区逐字不变。判据落在真正发出去的文本上——各段 token 数是减法算的，
    ceil 的舍入会让不相干的段跟着抖 1。"""
    if materials:
        assert "- 「os.pdf」" in flooded.messages[0].content
    if question:
        assert flooded.messages[-3].content == ordinary.messages[-3].content
    if evidence:
        assert flooded.messages[-1].content == ordinary.messages[-1].content
    if history:
        assert [item.content for item in flooded.messages[1:3]] == [item.content for item in ordinary.messages[1:3]]


# ---------------------------------------------------------------- 配额怎么来的

def test_the_soft_window_is_half_the_model_window(monkeypatch, tmp_path):
    """换一个窗口更小的模型只改这一个数，软窗口与各分区跟着一起缩。"""
    for name in ("AGENT_CONTEXT_TOKEN_LIMIT", "AGENT_HISTORY_TOKEN_BUDGET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "128000")

    # 用空目录当 project_root：本机 .env 里配着这两项，读进来就测不到推导。
    settings = Settings.from_environment(tmp_path)

    assert settings.agent_context_token_limit == 64_000
    assert settings.context_partitions.history == settings.agent_history_token_budget == 16_000


def test_the_default_window_keeps_todays_numbers(monkeypatch, tmp_path):
    """默认档必须和改造前逐字一致，否则这次改动会顺带改掉所有人的预算。"""
    for name in ("AGENT_MODEL_CONTEXT_WINDOW", "AGENT_CONTEXT_TOKEN_LIMIT", "AGENT_HISTORY_TOKEN_BUDGET"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_environment(tmp_path)

    assert settings.agent_context_token_limit == 512_000
    assert settings.agent_history_token_budget == 128_000


def test_an_explicit_soft_window_never_exceeds_the_model_window(monkeypatch, tmp_path):
    """配错了也不能让软窗口比真实窗口还大——那正是会被上游打回的那种配置。"""
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW", "32000")
    monkeypatch.setenv("AGENT_CONTEXT_TOKEN_LIMIT", "900000")

    assert Settings.from_environment(tmp_path).agent_context_token_limit == 32_000


def test_the_partitions_leave_room_for_output_and_error():
    """各分区加起来要小于软窗口：余下的留给模型输出与估算误差，不许分光。"""
    limits = PartitionLimits.from_window(512_000)
    used = sum((limits.system, limits.question, limits.history,
                limits.knowledge, limits.evidence, limits.skill))

    assert sum(CONTEXT_PARTITION_RATIOS.values()) < 1.0
    assert used < 512_000


# ---------------------------------------------------------------- 逐个分区

def test_a_giant_materials_list_is_cut_to_the_system_quota():
    """教材数量没有上界，清单能把系统提示撑爆。裁的只能是清单本身。"""
    limits = PartitionLimits.from_window(20_000)
    ordinary = assemble_messages(**{**ORDINARY, "limits": limits})
    flooded = assemble_messages(**{**ORDINARY, "limits": limits,
                                   "materials": [f"第{i}章讲义与习题解析合订本.pdf" for i in range(4_000)]})

    assert _segments(flooded)["context.segment.system"] <= limits.system
    assert "教材清单超出分区配额" in flooded.messages[0].content
    assert _clips(flooded)["context.segment.system"][0] > limits.system
    _untouched(flooded, ordinary, materials=False)


def test_an_overlong_question_is_cut_to_its_own_quota():
    """图片转录并进用户消息后，这一段可以任意长。"""
    limits = PartitionLimits.from_window(20_000)
    ordinary = assemble_messages(**{**ORDINARY, "limits": limits})
    huge = "请解释这张图" + "过程与线程的区别在这里详细展开。" * 4_000
    flooded = assemble_messages(**{**ORDINARY, "limits": limits, "question": huge, "seed_query": huge})

    assert _segments(flooded)["context.segment.question"] <= limits.question
    assert "本轮提问超出分区配额" in flooded.messages[-3].content
    assert _clips(flooded)["context.segment.question"][0] > limits.question
    _untouched(flooded, ordinary, question=False)


def test_oversized_seed_evidence_is_cut_to_the_evidence_quota():
    """检索片段的大小跟着 chunk_size 走，配大了整段证据能压过一切。"""
    limits = PartitionLimits.from_window(20_000)
    ordinary = assemble_messages(**{**ORDINARY, "limits": limits})
    flooded = assemble_messages(**{**ORDINARY, "limits": limits,
                                   "seed_result_text": "[1] os.pdf p.10：调度器要在公平与吞吐之间取舍。" * 3_000})
    evidence = _segments(flooded)["context.segment.evidence"] + _segments(flooded)["context.segment.wiki_evidence"]

    assert evidence <= limits.evidence
    assert "检索证据超出分区配额" in flooded.messages[-1].content
    _untouched(flooded, ordinary, evidence=False)


def test_a_huge_memory_file_is_cut_to_the_knowledge_quota():
    """长期记忆是用户自己在界面上编辑的 markdown，没有任何长度约束。"""
    limits = PartitionLimits.from_window(20_000)
    ordinary = assemble_messages(**{**ORDINARY, "limits": limits})
    flooded = assemble_messages(**{**ORDINARY, "limits": limits,
                                   "memory": "偏好：每次都要给例子，并且要写出推导过程。" * 3_000})
    knowledge = sum(_segments(flooded)[key] for key in
                    ("context.segment.memory", "context.segment.summary", "context.segment.wiki"))

    assert knowledge <= limits.knowledge
    assert "长期记忆超出分区配额" in flooded.messages[0].content
    _untouched(flooded, ordinary)


def test_the_wiki_directory_gives_way_before_the_memory_does():
    """同一分区内部也有先后：目录少列几页能用 wiki_index 补回来，用户手写的记忆补不回来。"""
    limits = PartitionLimits.from_window(20_000)
    memory = "偏好：讲解要给例子。" * 250
    flooded = assemble_messages(**{**ORDINARY, "limits": limits, "memory": memory,
                                   "wiki_entries": [(f"cpt_{i}", f"概念{i}") for i in range(60)]})

    assert sum(_segments(flooded)[key] for key in
               ("context.segment.memory", "context.segment.summary", "context.segment.wiki")) <= limits.knowledge
    assert memory in flooded.messages[0].content  # 记忆一个字没少
    assert flooded.messages[0].content.count("\n- cpt_") < 60


def test_the_general_mode_enforces_the_same_quotas():
    """通用模式没有教材段，但记忆和提问一样能撑爆。"""
    limits = PartitionLimits.from_window(20_000)
    assembled = assemble_general_messages(
        courses=["算法"], history=[], question="你好" * 40_000, history_token_budget=10_000,
        memory="偏好：讲解要给例子。" * 3_000, limits=limits,
    )
    segments = {item.key: item.tokens for item in assembled.segments}

    assert segments["context.segment.question"] <= limits.question
    assert segments["context.segment.memory"] <= limits.knowledge
    assert {item.key for item in assembled.clips} == {"context.segment.question", "context.segment.knowledge"}


# ---------------------------------------------------------------- 总闸

def _loop_messages(*, history: int = 0, tool_rounds: int = 0, tool_chars: int = 0,
                   seed_chars: int = 40) -> list[ChatMessage]:
    """照 assemble_messages 的形状搭一份，再补上工具循环追加的内容。"""
    messages = [ChatMessage(role="system", content="系统提示" * 20)]
    messages += [ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"第{i}轮" * 50)
                 for i in range(history)]
    messages.append(ChatMessage(role="user", content="本轮的问题"))
    messages.append(ChatMessage(role="assistant", content="",
                                tool_calls=(ToolCallRequest(SEED_CALL_ID, "search_materials", '{"query": "q"}'),)))
    messages.append(ChatMessage(role="tool", content="种" * seed_chars, tool_call_id=SEED_CALL_ID))
    for i in range(tool_rounds):
        messages.append(ChatMessage(role="assistant", content="",
                                    tool_calls=(ToolCallRequest(f"c{i}", "wiki_read", "{}"),)))
        messages.append(ChatMessage(role="tool", content=f"第{i}页" + "正" * tool_chars, tool_call_id=f"c{i}"))
    return messages


def test_the_tool_loop_cannot_grow_past_the_soft_window():
    """wiki_read 一轮能拿 10 × 6000 字符，只在组装时算一次挡不住。"""
    messages = _loop_messages(tool_rounds=10, tool_chars=6_000)
    assert message_tokens(messages) > 20_000  # 先证明确实撑爆了

    report = enforce_context_limit(messages, limit=20_000, history_count=0)

    assert message_tokens(messages) <= 20_000
    assert report.tools_cleared > 0 and report.triggered
    assert GATE_TOOL_NOTE in [item.content for item in messages]


def test_the_gate_never_touches_the_system_prompt_or_the_question():
    """这两段是这一轮要办的事本身，裁掉它们等于换了个任务。"""
    messages = _loop_messages(history=6, tool_rounds=12, tool_chars=8_000)
    system, question = messages[0].content, messages[7].content

    report = enforce_context_limit(messages, limit=3_000, history_count=6)

    assert report.triggered  # 先确认这一轮真的裁过，否则下面两条是空过
    assert messages[0].content == system
    assert any(item.role == "user" and item.content == question for item in messages)


def test_the_gate_spends_older_tool_results_before_history():
    """工具结果重新调一次就有，历史消息丢了就真没了。"""
    messages = _loop_messages(history=6, tool_rounds=6, tool_chars=4_000)

    report = enforce_context_limit(messages, limit=20_000, history_count=6)

    assert report.tools_cleared > 0
    assert report.history_dropped == 0
    assert [item.content for item in messages[1:7]] == [f"第{i}轮" * 50 for i in range(6)]


def test_the_gate_keeps_the_most_recent_tool_results():
    """砍掉模型正要用来作答的那几条，这一轮就白跑了。"""
    messages = _loop_messages(tool_rounds=8, tool_chars=5_000)

    enforce_context_limit(messages, limit=15_000, history_count=0)
    survivors = [item.content for item in messages if item.role == "tool" and item.content != GATE_TOOL_NOTE]

    assert len(survivors) == 3  # 种子 + 最近两条，其余六条已清空
    assert survivors[-2:] == ["第6页" + "正" * 5_000, "第7页" + "正" * 5_000]


def test_the_gate_falls_back_to_history_when_tool_results_are_not_enough():
    """工具结果全清完还是超，就得动历史；丢掉的条数要报出来。"""
    messages = _loop_messages(history=40, tool_rounds=1, tool_chars=100)

    report = enforce_context_limit(messages, limit=2_000, history_count=40)

    assert message_tokens(messages) <= 2_000
    assert report.history_dropped > 0
    assert len([item for item in messages if item.role in {"user", "assistant"}]) < 42


def test_the_gate_clips_seed_evidence_only_as_a_last_resort():
    """种子证据是回答要引用的东西，排在最后才动。"""
    lean = _loop_messages(seed_chars=20_000)

    report = enforce_context_limit(lean, limit=4_000, history_count=0)

    assert message_tokens(lean) <= 4_000
    assert report.evidence_clipped
    assert "种子检索证据的末尾已截断" in lean[-1].content


def test_the_gate_is_a_no_op_below_the_limit():
    """离限额还远的一轮，消息要逐字不变。"""
    messages = _loop_messages(history=4, tool_rounds=2, tool_chars=200)
    before = [(item.role, item.content) for item in messages]

    report = enforce_context_limit(messages, limit=512_000, history_count=4)

    assert [(item.role, item.content) for item in messages] == before
    assert not report.triggered


# ---------------------------------------------------------------- 工具定义与思考内容

MAIN_SPECS = specs_for(MAIN_PROFILE, capabilities=MAIN.capabilities)
NO_WIKI_SPECS = specs_for(without_tools(MAIN_PROFILE, WIKI_TOOLS), capabilities=MAIN.capabilities)


def test_the_tool_schema_is_part_of_what_this_turn_costs():
    """工具定义走 tools= 参数发出去，不在 messages 里，但每轮都发，一样占上游窗口。"""
    messages = _loop_messages()
    schema = tool_schema_tokens(MAIN_SPECS)

    assert schema > 2_000  # 十几份描述与 JSON Schema，比系统提示本身还大
    assert message_tokens(messages, MAIN_SPECS) == message_tokens(messages) + schema


def test_a_smaller_tool_set_costs_less():
    """工具集按轮变化，估算得跟着变：写死一个常量，撤掉 wiki_* 的那些轮就会报大。"""
    only_wiki = specs_for(tuple(sorted(WIKI_TOOLS)), capabilities=MAIN.capabilities)
    gap = tool_schema_tokens(MAIN_SPECS) - tool_schema_tokens(NO_WIKI_SPECS)

    assert gap > 0
    # 差额就是那两份定义本身（几个 token 的出入来自 JSON 分隔符），不是抹了个平均数
    assert abs(gap - tool_schema_tokens(only_wiki)) <= 5


def test_activating_a_skill_swaps_the_whole_schema():
    """profile 是整体替换而不是并集，schema 的量也跟着整体换掉。"""
    profile = profile_for_skill(("search_materials", "note_write"))
    specs = specs_for(profile.tools, capabilities=profile.capabilities)

    assert 0 < tool_schema_tokens(specs) < tool_schema_tokens(MAIN_SPECS)


def test_reasoning_is_charged_to_the_message_that_carries_it():
    """思考内容要原样回传给厂商，max 档下可能比正文还大。"""
    plain = [ChatMessage(role="assistant", content="结论。")]
    thinking = [replace(plain[0], reasoning="先看第一种可能，再看第二种。" * 200)]

    assert message_tokens(thinking) - message_tokens(plain) == estimate_tokens("先看第一种可能，再看第二种。" * 200)


def test_the_gate_counts_the_tool_schema_it_cannot_trim():
    """schema 裁不掉，但要算进总量：只算 messages 会以为还有余量，发出去就顶爆窗口。"""
    messages = _loop_messages(tool_rounds=3, tool_chars=1_000)
    limit = message_tokens(messages) + tool_schema_tokens(MAIN_SPECS) // 2

    assert not enforce_context_limit(list(messages), limit=limit, history_count=0).triggered
    report = enforce_context_limit(messages, limit=limit, history_count=0, tools=MAIN_SPECS)

    assert report.triggered
    assert message_tokens(messages, MAIN_SPECS) <= limit


def test_the_schema_counts_against_the_system_partition():
    """架构 §5.5：系统提示与 Tool Schema 共用一个配额，默认档下普通一轮离得还很远。"""
    assembled = assemble_messages(**{**ORDINARY, "limits": PRODUCTION, "tools": MAIN_SPECS})
    segments = _segments(assembled)

    assert segments["context.segment.tools"] == tool_schema_tokens(MAIN_SPECS)
    assert sum(item.tokens for item in assembled.segments) == message_tokens(assembled.messages, MAIN_SPECS)
    assert assembled.clips == ()
    assert segments["context.segment.system"] + segments["context.segment.tools"] < PRODUCTION.system // 4


def test_a_tight_system_partition_makes_room_for_the_schema():
    """同一份输入只多下发工具定义，这一段就该更早开始裁——共用配额才是真的在共用。"""
    limits = PartitionLimits.from_window(40_000)
    crowded = {**ORDINARY, "limits": limits, "materials": [f"第{i}章讲义.pdf" for i in range(200)]}

    plain = assemble_messages(**crowded)
    with_tools = assemble_messages(**{**crowded, "tools": MAIN_SPECS})

    assert plain.clips == ()
    assert [item.key for item in with_tools.clips] == ["context.segment.system"]
    assert len(with_tools.messages[0].content) < len(plain.messages[0].content)


# ---------------------------------------------------------------- 正常轮次不受影响

def test_an_ordinary_turn_is_identical_with_and_without_quotas():
    """普通一轮离限额差得远，开不开配额都必须逐字一样。"""
    capped = assemble_messages(**{**ORDINARY, "limits": PRODUCTION})
    uncapped = assemble_messages(**{**ORDINARY, "limits": PartitionLimits.from_window(10_000_000)})

    assert [(item.role, item.content) for item in capped.messages] == \
           [(item.role, item.content) for item in uncapped.messages]
    assert capped.clips == () and uncapped.clips == ()


def test_a_realistic_wiki_turn_still_fits_every_partition():
    """开了知识页目录的真实一轮，各分区都要离配额很远，别刚上线就天天在裁。"""
    assembled = assemble_messages(**{
        **ORDINARY, "limits": PRODUCTION,
        "wiki_entries": [(f"cpt_{i}", f"操作系统概念{i}") for i in range(60)],
        "skill_summaries": "practice：出题与批改\nresearch：联网查资料",
        "practice_digest": "有 2 道题尚未批改。",
    })
    segments = _segments(assembled)

    assert assembled.clips == ()
    assert segments["context.segment.system"] < PRODUCTION.system // 4
    assert sum(segments[key] for key in ("context.segment.memory", "context.segment.summary",
                                         "context.segment.wiki")) < PRODUCTION.knowledge // 4


# ---------------------------------------------------------------- 真的接进了这一轮

def _settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    return Settings(
        data_dir=data_dir, database_path=data_dir / "coursepilot.db", uploads_dir=data_dir / "materials",
        text_provider="example", text_base_url="https://api.example.com/v1", text_api_key="",
        text_model="example-model", enable_remote_llm=False, chunk_size=1_500, chunk_overlap=0, top_k_results=6,
    )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as test_client:
        yield test_client


def _events(body: str) -> list[tuple[str, dict]]:
    frames = [frame for frame in body.split("\n\n") if frame]
    return [(frame.splitlines()[0].removeprefix("event: "),
             json.loads(frame.splitlines()[1].removeprefix("data: "))) for frame in frames]


class _Scripted:
    mode, provider, model = "provider", "example", "example-model"

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[list[ChatMessage]] = []
        self.tools: list[tuple] = []

    def chat(self, *, messages, tools=()):
        self.calls.append(list(messages))
        self.tools.append(tuple(tools))
        yield from self._script.pop(0)

    def health(self):
        return {}

    def close(self):
        return None


def test_the_gate_runs_on_every_round_of_a_real_turn(client):
    """总闸接在工具循环里，不是一个没人调的函数。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    text = "\n\n".join(f"第 {i} 节：调度器要在公平与吞吐之间取舍。" * 60 for i in range(1, 12))
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": ("os.md", text, "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/v2/jobs/{job}").json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]

    scripted = _Scripted([
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "调度"}'),))],
        [ChatToolCalls((ToolCallRequest("c2", "search_materials", '{"query": "吞吐"}'),))],
        [ChatDelta("取舍在公平与吞吐之间。[1]"),
         ChatFinal("取舍在公平与吞吐之间。[1]", "stop", "example", "example-model", "provider")],
    ])
    turns = workspace(client).turns
    turns._responder = scripted
    turns._context_token_limit = 6_000

    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "gate-1", "message": "调度怎么取舍？"}).text)
    usage = [data for name, data in events if name == "context_usage"]

    # 不设闸时最后一次请求会带着三轮检索结果，远超 6000
    assert message_tokens(scripted.calls[-1]) <= 6_000
    assert usage[-1]["total_tokens"] <= usage[-1]["limit_tokens"]
    assert usage[-1]["gate_tools_cleared"] > 0 or usage[-1]["gate_evidence_clipped"]


def test_the_breakdown_reports_what_was_actually_sent(client):
    """总闸裁过之后还照组装时的数字报，用户会以为那几条历史与证据都还在。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    text = "\n\n".join(f"第 {i} 节：调度器要在公平与吞吐之间取舍。" * 60 for i in range(1, 12))
    material = client.post(f"/api/v2/courses/{course['id']}/materials",
                           files={"file": ("os.md", text, "text/markdown")}).json()
    job = client.post(f"/api/v2/materials/{material['id']}/index").json()["id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and client.get(f"/api/v2/jobs/{job}").json()["status"] not in {"completed", "failed"}:
        time.sleep(0.01)
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]

    turns = workspace(client).turns
    turns._context_token_limit = 5_000
    for index in range(2):
        turns._responder = _Scripted([[ChatDelta("好的。"), ChatFinal("好的。", "stop", "example", "example-model", "provider")]])
        client.post(f"/api/v2/sessions/{session_id}/turns",
                    json={"client_request_id": f"warm-{index}", "message": f"第 {index} 个问题：调度怎么取舍？"})

    scripted = _Scripted([
        [ChatToolCalls((ToolCallRequest("c1", "search_materials", '{"query": "调度"}'),))],
        [ChatDelta("取舍在公平与吞吐之间。[1]"),
         ChatFinal("取舍在公平与吞吐之间。[1]", "stop", "example", "example-model", "provider")],
    ])
    turns._responder = scripted
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "report-1", "message": "再说一遍调度怎么取舍？"}).text)
    last = [data for name, data in events if name == "context_usage"][-1]

    assert last["total_tokens"] == message_tokens(scripted.calls[-1], scripted.tools[-1])
    # 各段之和不许超过真实总量：超了就说明报了上下文里已经没有的东西
    assert sum(item["tokens"] for item in last["segments"]) <= last["total_tokens"]


def test_the_breakdown_shows_what_the_tool_definitions_cost(client):
    """固定开销也要看得见：用户改不动它，但得知道这一轮为什么起步就几千 token。"""
    course = client.post("/api/v2/courses", json={"name": "操作系统"}).json()
    session_id = client.post("/api/v2/sessions", json={"scope_mode": "course", "course_id": course["id"]}).json()["id"]

    scripted = _Scripted([[ChatDelta("先来先服务会堵住短作业。"),
                           ChatFinal("先来先服务会堵住短作业。", "stop", "example", "example-model", "provider")]])
    workspace(client).turns._responder = scripted
    events = _events(client.post(f"/api/v2/sessions/{session_id}/turns",
                                 json={"client_request_id": "tools-1", "message": "护航效应是什么？"}).text)
    last = [data for name, data in events if name == "context_usage"][-1]
    reported = next(item for item in last["segments"] if item["label_key"] == "context.segment.tools")

    assert scripted.tools[-1]  # 这一轮确实下发了工具，否则下面两条是空过
    assert reported["tokens"] == tool_schema_tokens(scripted.tools[-1]) > 1_000
    assert last["total_tokens"] == message_tokens(scripted.calls[-1], scripted.tools[-1])
