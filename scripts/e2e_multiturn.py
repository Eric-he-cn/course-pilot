"""端到端：需要多轮对话才能完成的复杂任务。

和 e2e_journey.py 的区别：journey 是一轮一个能力点；这里每个场景都是**同一件事被拆到几轮里**，
后一轮必须依赖前一轮的产物（题目、批改结果、旧计划、上一句里的指代、写进记忆的偏好）。
断言只看结构化行为（工具调用、SSE 事件、落库数据），不断言回答措辞。

    CP_PORT_OFFSET=1 STORAGE_DATA_DIR=testdata/e2e ./scripts/dev.sh
    .venv/bin/python scripts/e2e_multiturn.py --base http://127.0.0.1:8001 --data-dir testdata/e2e
    .venv/bin/python scripts/e2e_multiturn.py --only A,C          # 只跑某几个场景

数据目录得是新布局（<data>/users/<user_id>/），旧布局先跑 scripts/migrate_to_users.py。
场景 D/E/F/G 依赖「大语言模型」「操作系统」两门已索引的课程。
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from core.identity import sole_workspace  # noqa: E402

results: list[tuple[str, bool, str]] = []
BASE = ""
DATA = ROOT / "testdata" / "e2e"


def call(path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{BASE}/api/v2{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


class Turn:
    def __init__(self, raw: str) -> None:
        self.events: list[tuple[str, dict]] = []
        for frame in raw.split("\n\n"):
            lines = [line for line in frame.splitlines() if line.strip()]
            if len(lines) < 2:
                continue
            try:
                self.events.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
            except json.JSONDecodeError:
                continue

    def named(self, name: str) -> list[dict]:
        return [data for event, data in self.events if event == name]

    @property
    def answer(self) -> str:
        return "".join(item.get("text", "") for item in self.named("text_delta"))

    @property
    def tools(self) -> list[str]:
        return [item["name"] for item in self.named("tool_call")]

    @property
    def choices(self) -> list[str]:
        events = self.named("choices")
        return events[-1].get("options", []) if events else []

    @property
    def citations(self) -> list[dict]:
        return self.named("citation")

    @property
    def finish_reason(self) -> str:
        done = self.named("turn_completed")
        return done[-1].get("finish_reason", "") if done else ""

    @property
    def course_name(self) -> str | None:
        got = self.named("course_resolution")
        return got[0].get("course_name") if got else None


def ask(session_id: str, message: str, tag: str) -> Turn:
    started = time.monotonic()
    payload = json.dumps({"message": message, "client_request_id": tag}).encode()
    request = urllib.request.Request(
        f"{BASE}/api/v2/sessions/{session_id}/turns", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=900) as response:
        turn = Turn(response.read().decode())
    print(f"    ↳ 「{message[:34]}」{time.monotonic() - started:.0f}s 工具={turn.tools or '无'}")
    return turn


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((name, bool(condition), detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(sole_workspace(DATA) / "coursepilot.db")
    connection.row_factory = sqlite3.Row
    return connection


def scalar(sql: str, *args) -> int:
    with db() as connection:
        return connection.execute(sql, args).fetchone()[0]


def course_id(name: str) -> str:
    return next(c["id"] for c in call("/courses") if c["name"] == name)


# ---------------------------------------------------------------- 场景 A
def scenario_options(course: str) -> None:
    """含糊的大任务 → 模型给选项 → 点选项 → 必须接着做，而不是从头再问。"""
    print("\n[A] 选项式反问：给选项 → 点一下 → 接着做原任务")
    session = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    # 数据目录复用，这门课的计划可能已经排到考试日了。记下起始版本，最后才判得出
    # 「本轮真的写了」——只看「有没有计划」会靠上一次跑的数据空过。
    existing = call(f"/courses/{course}/plan")["plan"]
    version_at_start = existing["version"] if existing else 0
    first = ask(session, "下周我要考这门课，帮我准备一下", "mt-a1")
    # 缺关键信息时要先问清楚，不能自己编一个日期就开排。用不用按钮由模型自己判断：
    # 选择题那条路有服务端补救轮兜着，澄清式反问没有，实测约五次里有一次是纯文字问的。
    check("信息不全时先问清楚，不自己瞎编", "plan_update" not in first.tools, str(first.tools))
    if first.choices:
        check("选项数量在 2~4 个", 2 <= len(first.choices) <= 4, str(first.choices))
        # 点一下等于把这句话当用户消息发出去，所以选项必须是答案；写成问题就是把问题问回给自己
        check("选项是答案不是问题", not any("？" in item or "?" in item for item in first.choices), str(first.choices))
        check("选项落库到消息上", bool(call(f"/sessions/{session}/messages")["messages"][-1].get("choices")))
    else:
        print(f"    （这一轮没给按钮，改成正文提问：{first.answer[:60]!r}）")

    picked = first.choices[0] if first.choices else "8月3日考，考全部内容"
    second = ask(session, picked, "mt-a2")
    # 把选中的那句复述一遍是好行为（确认收到），所以只查它没有再摆一次选项。
    # 上一轮是正文提问时这条不查：那时回的是写死的一句，未必答得上它实际问的问题，
    # 模型接着问反而是对的。
    if first.choices:
        check("回答之后不再重复问同一件事", "ask_user" not in second.tools, f"答了「{picked}」，工具={second.tools}")
    else:
        print("    （跳过「回答之后不再重复问同一件事」：上一轮是正文提问，回的这句未必对得上）")
    check("点选项后接着推进",
          bool(set(second.tools) & {"use_skill", "plan_update", "search_materials", "note_write", "get_archive"}),
          f"工具只有 {second.tools}")

    # 缺的信息补齐后必须真落库：多轮任务的终点是产物，不是又一轮对话。信息够了就动手，
    # 所以哪一轮写不固定——第二轮就写完的话，第三轮只读一下计划是对的。
    # 要求写成现有计划满足不了的样子（每天 2 小时、周末也排），否则模型读一眼发现
    # 「已经排好了」就不写，那是对的行为，判据却会红。
    third = ask(session, "8 月 20 号考，每天改成能看 2 小时、周末也照排，直接排进系统不用再确认", "mt-a3")
    check("授权明确后没被确认闸门挡住", "plan_update" in second.tools + third.tools,
          f"两轮的工具：{second.tools} / {third.tools}")
    plan = call(f"/courses/{course}/plan")["plan"]
    check("三轮下来本轮真的写进了计划", bool(plan and plan["items"]) and plan["version"] > version_at_start,
          f"版本 {version_at_start} → {plan['version'] if plan else None}")


# ---------------------------------------------------------------- 场景 B
def scenario_practice(course: str) -> None:
    """出题 → 作答 → 追问某一题 → 针对薄弱点再出题。四轮共享同一批题目。"""
    print("\n[B] 练习闭环四轮：出题 → 作答 → 追问 → 补练")
    session = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    before_events = scalar("SELECT count(*) FROM evidence_events WHERE course_id = ?", course)
    before_artifacts = scalar("SELECT count(*) FROM artifacts WHERE course_id = ?", course)

    quiz = ask(session, "出3道题考考我，一道选择题两道简答", "mt-b1")
    check("出题加载了练习规程", "use_skill" in quiz.tools, str(quiz.tools))
    check("出题写了 artifact", scalar("SELECT count(*) FROM artifacts WHERE course_id = ?", course) > before_artifacts)

    graded = ask(session, "第一题我选C；第二题我觉得是让梯度更稳定；第三题不会，跳过", "mt-b2")
    events = scalar("SELECT count(*) FROM evidence_events WHERE course_id = ?", course)
    check("作答产生证据事件", events > before_events, f"{before_events} → {events}")
    with db() as connection:
        kinds = [r["kind"] for r in connection.execute(
            "SELECT kind FROM evidence_events WHERE course_id = ? ORDER BY created_at DESC LIMIT 6", (course,))]
    check("批改区分了对错", len(set(kinds) & {"attempt_correct", "attempt_incorrect"}) >= 1, str(kinds))
    check("三题都留了证据", events - before_events >= 3, f"新增 {events - before_events} 条")

    followup = ask(session, "第三题的答案再讲细一点，我完全没思路", "mt-b3")
    check("追问不需要重述题目就能接上", len(followup.answer) > 120 and "哪一题" not in followup.answer[:40],
          followup.answer[:80])
    check("追问回到教材取证", bool(followup.citations) or "search_materials" in followup.tools,
          str(followup.tools))

    more = ask(session, "就照我刚才错的那块，再出2道同类的题", "mt-b4")
    check("补练仍走练习规程", "use_skill" in more.tools or "artifact_append" in more.tools, str(more.tools))
    check("补练读了我的作答记录", bool(set(more.tools) & {"get_archive", "search_materials"}), str(more.tools))


# ---------------------------------------------------------------- 场景 C
def is_weekend_ahead(item: dict, today: str) -> bool:
    return item["due_date"] >= today and time.strptime(item["due_date"], "%Y-%m-%d").tm_wday >= 5


def scenario_plan(course: str) -> None:
    """排计划 → 提出约束改计划。第二轮必须先读旧计划再改，不能当新计划重排。"""
    print("\n[C] 计划两轮：排出来 → 按新约束改")
    session = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    # 数据目录复用，库里可能已经有上一次跑出来的计划。记下起始版本，首轮才能判「真的写进去了」。
    existing = call(f"/courses/{course}/plan")["plan"]
    version_at_start = existing["version"] if existing else 0
    # 首轮点明周末也排：不然模型常自己避开周末，第二轮就没东西可匀，那条判据白过。
    made = ask(session, "我 8 月 20 号考这门，范围就是资料库里的内容，从明天开始每天 1.5 小时，"
                        "周末也照排不休息，直接排进系统排一份复习计划，不用再问我", "mt-c1")
    check("排计划调了 plan_update", "plan_update" in made.tools, str(made.tools))
    plan = call(f"/courses/{course}/plan")["plan"]
    # 光看「有没有条目」不够：首轮压根没写时库里留着上一次的计划，照样绿，
    # 后面几条判据就顺着旧数据往下走，失败被藏起来。
    if not check("首轮计划真的写进去了",
                 bool(plan and plan["items"]) and plan["version"] > version_at_start,
                 f"版本 {version_at_start} → {plan['version'] if plan else None}"):
        return
    version_before = plan["version"]
    revisions_before = scalar("SELECT count(*) FROM plan_revisions WHERE plan_id = ?", plan["id"])
    # plan_update 的契约是重写今天及以后，过去日期它无权改动；复用的数据目录里会攒下
    # 往日跑出来的周末条目，把它们算进来这条断言就永远红。
    today = time.strftime("%Y-%m-%d")
    weekend_before = sum(1 for item in plan["items"] if is_weekend_ahead(item, today))
    count_before = len(plan["items"])

    # 说清是「以后每个周末」：只说「周末我要出差」可以读成只指这一个周末，那时判据去查
    # 后面几个周末就等于把模型的一种合法理解判成错。
    changed = ask(session, "从现在到考试，每个周六周日我都在出差看不了书，"
                           "把所有周末的内容都匀到工作日去", "mt-c2")
    check("改计划先读了旧计划", "get_plan" in changed.tools, str(changed.tools))
    check("改计划走的是 plan_update", "plan_update" in changed.tools, str(changed.tools))
    after = call(f"/courses/{course}/plan")["plan"]
    written = check("计划版本递增", after["version"] > version_before, f"{version_before} → {after['version']}")
    check("改动留了 revision",
          scalar("SELECT count(*) FROM plan_revisions WHERE plan_id = ?", plan["id"]) > revisions_before)
    # 删掉周末条目、或者留一条「出差跳过」的占位标记，都算满足约束；不能满足的是
    # 周末还挂着真的学习任务。占位标记短且不挂概念，正经任务是几十字加 concept_id。
    weekend = [item for item in after["items"] if is_weekend_ahead(item, today)]
    still_working = [f"{item['due_date']} {item['title']}" for item in weekend
                     if len(item["title"]) >= 12 or item.get("concept_id")]
    if not written:
        # 这轮压根没写进去，周末当然还挂着——报出来会把失败归因指错地方。
        print("    （跳过「周末不再安排学习内容」：这轮没有写入，先看上面那条）")
    elif weekend_before:
        check("周末不再安排学习内容", not still_working, f"周末还挂着任务：{still_working}")
        print(f"    （周末留了 {len(weekend)} 条占位：{[i['title'] for i in weekend][:2]}）")
    else:
        # 首版计划本来就没排到周末时，这条判据没有被考验到，报成通过会掩盖这一点。
        print("    （跳过「周末不再安排学习内容」：首版计划就没排到周末，没什么可匀的）")
    # 条目数不作断言：合并天数并提高每日时长、或留一条占位标记，都是合法的匀法。
    print(f"    （条目 {count_before} → {len(after['items'])} 条，其中原周末 {weekend_before} 条）")


# ---------------------------------------------------------------- 场景 D
def scenario_reference(course: str) -> None:
    """指代消解：第二轮只说「刚才第二点」，模型得自己回看上一轮说了什么。"""
    print("\n[D] 指代消解：只说「刚才那第二点」")
    session = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    first = ask(session, "这门课里注意力机制的核心要点有哪几条？分条说", "mt-d1")
    check("首轮带教材引用", bool(first.citations), f"{len(first.citations)} 条")

    second = ask(session, "第二条我没看懂，换个说法再讲一遍，顺便举个具体例子", "mt-d2")
    check("指代能接上，不反问「哪一条」",
          "第二条" not in second.answer[:30] or len(second.answer) > 200,
          second.answer[:80])
    check("指代轮不跑偏到别的课", second.course_name in (None, "大语言模型"), str(second.course_name))
    check("展开这一轮仍然给依据", bool(second.citations) or "search_materials" in second.tools, str(second.tools))


# ---------------------------------------------------------------- 场景 E
def scenario_memory(course: str) -> None:
    """跨会话记忆：这一轮说的偏好，下一个新会话要还认得。"""
    print("\n[E] 跨会话记忆：说一次偏好，换会话还记得")
    memory_file = sole_workspace(DATA) / "user.md"
    # 先抹掉上次跑留下的同一条：不清理的话 memory_patch 幂等会让「文件没变」看起来像没写成
    if memory_file.is_file():
        kept = [line for line in memory_file.read_text().splitlines()
                if "数学" not in line and "公式" not in line]
        memory_file.write_text("\n".join(kept))
    before = memory_file.read_text() if memory_file.is_file() else ""
    session = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    told = ask(session, "记一下我的情况：我本科是数学系的，以后讲原理直接上公式推导，别用生活类比", "mt-e1")
    check("偏好写进了记忆", "memory_patch" in told.tools, str(told.tools))
    after = memory_file.read_text() if memory_file.is_file() else ""
    check("记忆文件真的变了", after != before, f"{len(before)} → {len(after)} 字")
    check("记下的是偏好本身", "数学" in after or "公式" in after, after[-200:])

    fresh = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    later = ask(fresh, "讲讲 softmax 温度系数是怎么起作用的", "mt-e2")
    # 段落列表恒有「长期记忆」一项，所以要看它的字数，不是看标签在不在
    segments = {s["label"]: s["tokens"] for item in later.named("context_usage") for s in item.get("segments", [])}
    check("新会话把记忆带进了上下文", segments.get("长期记忆", 0) > 0, str(segments))


# ---------------------------------------------------------------- 场景 F
def scenario_switch(first_course: str, second_course: str) -> None:
    """通用会话里连着问两门课，解析要跟着走，别把上一门的上下文糊过来。"""
    print("\n[F] 通用会话里跨课切换")
    session = call("/sessions", {"scope_mode": "general"})["id"]
    one = ask(session, "指令微调和预训练的目标函数差在哪？", "mt-f1")
    check("第一轮解析到大语言模型", one.course_name == "大语言模型", str(one.course_name))

    # 选操作系统课**确实有**的内容：切不过去的代价才看得见——资料就在隔壁课，
    # 用户拿到的却是没有依据的通用回答。
    two = ask(session, "换个话题，FIFO 调度为什么会有护航效应？", "mt-f2")
    check("换话题后解析跟着切走", two.course_name == "操作系统", str(two.course_name))
    print(f"    （切过去后引用 {len(two.citations)} 条——这门课只有 62 块，命中数会浮动，不作断言）")

    # 回指句没有学科信号，解析器只看当前这句，所以课程停在最近切到的那门（已知取舍）。
    # 能保证的是回答对准原问题——那是从会话历史里认出来的，不依赖检索范围。
    back = ask(session, "回到我最开始问的那个问题，再补充两句", "mt-f3")
    check("说「最开始那个问题」答的是那个问题",
          "指令微调" in back.answer or "预训练" in back.answer, back.answer[:80])


# ---------------------------------------------------------------- 场景 G
_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")
_QUOTE_LINE = re.compile(r"(?m)^\s*>.*$")
# 公式和代码里的标识符（softmax、mathbb、W^Q）全是拉丁字母，但它们和回答语言无关：
# 公式密集的中文回答照样能刷到 0.6 以上。剔掉之后剩下的才是散文部分。
_NOISE = (
    re.compile(r"```.*?```", re.S), re.compile(r"\$\$.*?\$\$", re.S),
    re.compile(r"\$[^$\n]+\$"), re.compile(r"`[^`\n]+`"),
)
# 判英文答要用占比而不是「有没有汉字」：中文课程名、中文教材术语、照抄不翻译的原文片段
# 都会合法地带进汉字。拉丁字母在「字母+汉字」里的占比对这些都不敏感。
# 实测（剔公式后）：英文答 0.998~1.000，中文答 0.036~0.127，所以门槛取 0.60。
_ENGLISH_MIN = 0.60


def latin_share(text: str) -> float:
    """拉丁字母占「拉丁字母 + 汉字」的比例。引用块、公式、代码先剔掉——照抄的中文原文和
    公式里的标识符都不代表回答语言。"""
    body = _QUOTE_LINE.sub("", text)
    for pattern in _NOISE:
        body = pattern.sub("", body)
    latin, cjk = len(_LATIN.findall(body)), len(_CJK.findall(body))
    return latin / (latin + cjk) if latin + cjk else 0.0


def scenario_language(course: str) -> None:
    """回答语言跟着这一轮提问的语言走，和界面语言无关。只看占比，不看措辞。"""
    print("\n[G] 回答语言跟随提问语言")
    general = call("/sessions", {"scope_mode": "general"})["id"]
    hello = ask(general, "hello, what can you do?", "mt-g1")
    share = latin_share(hello.answer)
    check("通用会话英文提问用英文答", share > _ENGLISH_MIN,
          f"拉丁字母占比 {share:.3f}，回答 {hello.answer[:80]!r}")
    print(f"    （通用轮拉丁字母占比 {share:.3f}）")

    session = call("/sessions", {"scope_mode": "course", "course_id": course})["id"]
    asked = ask(session, "Please explain how multi-head attention differs from single-head attention.", "mt-g2")
    share = latin_share(asked.answer)
    check("课程会话英文提问用英文答", share > _ENGLISH_MIN,
          f"拉丁字母占比 {share:.3f}，回答 {asked.answer[:80]!r}")
    print(f"    （课程轮拉丁字母占比 {share:.3f}，引用 {len(asked.citations)} 条）")


def main() -> int:
    global BASE, DATA
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--data-dir", default="testdata/e2e")
    parser.add_argument("--only", default="", help="只跑某几个场景，例如 A,C")
    args = parser.parse_args()
    BASE, DATA = args.base, ROOT / args.data_dir

    llm = course_id("大语言模型")
    operating = course_id("操作系统")
    scenarios = {
        "A": lambda: scenario_options(llm),
        "B": lambda: scenario_practice(llm),
        "C": lambda: scenario_plan(llm),
        "D": lambda: scenario_reference(llm),
        "E": lambda: scenario_memory(llm),
        "F": lambda: scenario_switch(llm, operating),
        "G": lambda: scenario_language(llm),
    }
    wanted = [k.strip().upper() for k in args.only.split(",") if k.strip()] or list(scenarios)
    for key in wanted:
        try:
            scenarios[key]()
        except urllib.error.HTTPError as error:
            results.append((f"场景 {key} 中断", False, f"HTTP {error.code} {error.read().decode()[:200]}"))
        except Exception as error:  # noqa: BLE001 - 场景挂了也算失败，不让它吞掉后面的场景
            results.append((f"场景 {key} 中断", False, f"{type(error).__name__} {error}"))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 60}\n{passed}/{len(results)} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name} — {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
