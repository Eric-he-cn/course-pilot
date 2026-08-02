"""端到端用户旅程：从空库开始，模拟一个真实用户连续多轮使用，逐项断言。

与 benchmark.py 的区别：benchmark 是每条用例独立的冒烟检查；这里是一条**有状态的连贯旅程**，
后一步依赖前一步的产物（作答依赖出题、复盘依赖错题、复习计划依赖掌握度）。

用法：
    STORAGE_DATA_DIR=testdata/e2e-fresh .venv/bin/python -m uvicorn app.main:app \
        --app-dir backend --host 127.0.0.1 --port 8001
    .venv/bin/python scripts/e2e_journey.py --base http://127.0.0.1:8001 --data-dir testdata/e2e-fresh

断言的是结构化行为（SSE 事件、落库数据、工具调用），不断言回答文本——
模型换措辞不该让测试假失败。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "testdata" / "fixtures"
sys.path.insert(0, str(ROOT / "backend"))

from core.identity import sole_workspace  # noqa: E402

results: list[tuple[str, bool, str]] = []


def call(base: str, path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{base}/api/v2{path}", data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def upload(base: str, course_id: str, path: Path) -> dict:
    boundary = "----coursepilot-journey"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        b"Content-Type: application/pdf\r\n\r\n",
        path.read_bytes(), b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        f"{base}/api/v2/courses/{course_id}/materials", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode())


class Turn:
    """一轮对话的结构化结果。"""

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
    def finish_reason(self) -> str:
        completed = self.named("turn_completed")
        return completed[-1].get("finish_reason", "") if completed else ""

    @property
    def course_name(self) -> str | None:
        resolution = self.named("course_resolution")
        return resolution[0].get("course_name") if resolution else None


def ask(base: str, session_id: str, message: str, tag: str) -> Turn:
    payload = json.dumps({"message": message, "client_request_id": tag}).encode()
    request = urllib.request.Request(
        f"{base}/api/v2/sessions/{session_id}/turns", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        return Turn(response.read().decode())


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((name, condition, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f" — {detail}" if detail and not condition else ""))
    return condition


def workspace(data_dir: Path) -> Path:
    """库、笔记、trace 都在 <data>/users/<user_id>/ 下，不在 <data>/ 根上。"""
    return sole_workspace(data_dir)


def db(data_dir: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(workspace(data_dir) / "coursepilot.db")
    connection.row_factory = sqlite3.Row
    return connection


def wait_job(base: str, job_id: str, *, timeout: int = 900) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = call(base, f"/jobs/{job_id}")
        if job["status"] in {"completed", "failed"}:
            return job
        time.sleep(3)
    return {"status": "timeout", "error": f"{timeout}s 内没进终态"}


def seed_citations(turn: Turn) -> list[dict]:
    """种子检索那一次登记的引用：从 origin=seed 的 tool_call 到紧随其后的 tool_result 之间。

    模型自己再检索一次会追加别的引用，混在一起就比不出「教材席位有没有被挤掉」。
    """
    picked: list[dict] = []
    inside = False
    for name, data in turn.events:
        if name == "tool_call":
            inside = data.get("origin") == "seed"
        elif name == "tool_result":
            if inside:
                break
        elif name == "citation" and inside:
            picked.append(data)
    return picked


def citation_key(item: dict) -> tuple:
    return (item.get("kind"), item.get("document"), item.get("page"), item.get("chunk_id"))


def journey(base: str, data_dir: Path, *, wiki_budget: int) -> None:
    # ---- 第 1 步：建课并上传教材 ----
    print("\n[1] 新建课程与教材索引")
    course = call(base, "/courses", {"name": "操作系统"})
    other = call(base, "/courses", {"name": "深度学习"})
    check("建课返回稳定颜色", bool(course.get("color", "").startswith("#")), str(course))

    material = upload(base, course["id"], FIXTURES / "os-cpu-scheduling.pdf")
    job = call(base, f"/materials/{material['id']}/index", {})
    status = wait_job(base, job["id"])["status"]
    check("教材索引完成", status == "completed", f"job 状态 {status}")
    materials = call(base, f"/courses/{course['id']}/materials")
    indexed = next((m for m in materials if m["id"] == material["id"]), {})
    check("索引产出切块与向量", (indexed.get("chunk_count") or 0) > 0 and (indexed.get("embedded_count") or 0) > 0,
          f"chunks={indexed.get('chunk_count')} embedded={indexed.get('embedded_count')}")

    # 第二门课上传另一学科教材，用于验证课程隔离
    other_material = upload(base, other["id"], FIXTURES / "深度学习-批量规范化.pdf")
    other_job = call(base, f"/materials/{other_material['id']}/index", {})
    wait_job(base, other_job["id"])

    # ---- 第 2 步：课程会话里提问，要求带引用 ----
    print("\n[2] 课程会话提问与取证")
    session = call(base, "/sessions", {"scope_mode": "course", "course_id": course["id"]})
    turn = ask(base, session["id"], "FIFO 调度为什么会有护航效应？", "j-ask")
    check("回答带教材引用", len(turn.named("citation")) > 0 and "[" in turn.answer)
    check("首轮先做种子检索", turn.tools[:1] == ["search_materials"], str(turn.tools))
    check("上下文构成已上报", any(item.get("segments") for item in turn.named("context_usage")))

    # ---- 第 3 步：练习闭环（出题 → 作答 → 归因） ----
    print("\n[3] 练习闭环")
    quiz = ask(base, session["id"], "出两道题考考我", "j-quiz")
    check("出题自动加载 practice", "use_skill" in quiz.tools, str(quiz.tools))
    before = db(data_dir).execute("SELECT count(*) c FROM evidence_events").fetchone()["c"]
    answer = ask(base, session["id"], "第一题我觉得是先来先服务，第二题不会", "j-answer")
    rows = db(data_dir).execute("SELECT kind, attribution_status FROM evidence_events").fetchall()
    check("作答产生证据事件", len(rows) > before, f"{before} → {len(rows)}")
    check("证据归因到概念目录", any(r["attribution_status"] == "attributed" for r in rows),
          str([dict(r) for r in rows]))
    check("答错被记为 incorrect", any(r["kind"] == "attempt_incorrect" for r in rows),
          str([r["kind"] for r in rows]))
    del answer

    # ---- 第 4 步：掌握度与档案 ----
    print("\n[4] 学习档案")
    archive = call(base, f"/courses/{course['id']}/archive")
    check("档案有证据事件", archive["evidence_count"] > 0)
    check("掌握度证据不足时不给分数",
          all(item["score"] is None or item["objective_events"] >= 3 for item in archive["mastery"]),
          str(archive["mastery"]))

    # ---- 第 5 步：排计划（写操作 + 版本化） ----
    print("\n[5] 学习计划")
    # 要把考试范围说清楚。只说「排个计划」时模型会先反问范围（资料库只有一章，它无法
    # 判断该覆盖到哪），那是合理行为，不该拿它当失败。
    plan_turn = ask(
        base, session["id"],
        "我 8 月 10 号考这门课，考试范围就是资料库里这一章 CPU 调度，我还没开始复习。"
        "直接排一份从明天到考试前的计划写进系统，每天 2 小时，不用再问我",
        "j-plan",
    )
    check("排计划调用了 plan_update", "plan_update" in plan_turn.tools, str(plan_turn.tools))
    plan = call(base, f"/courses/{course['id']}/plan")["plan"]
    check("计划已落库且有版本", bool(plan) and plan["version"] >= 1, str(plan and plan["version"]))
    check("计划条目不早于今天", bool(plan) and all(item["due_date"] >= time.strftime("%Y-%m-%d") for item in plan["items"]),
          str([i["due_date"] for i in (plan or {}).get("items", [])][:3]))
    revisions = db(data_dir).execute("SELECT count(*) c FROM plan_revisions").fetchone()["c"]
    check("计划改动留下 revision", revisions >= 1, f"{revisions} 条")

    # ---- 第 6 步：图示与笔记 ----
    print("\n[6] 图示与笔记")
    diagram = ask(base, session["id"], "画一张流程图讲清 STCF 的抢占判断", "j-diagram")
    check("图示输出 mermaid 代码块", "```mermaid" in diagram.answer)
    cards = ask(base, session["id"], "把 FIFO 和 SJF 做成学习卡片存起来", "j-cards")
    check("卡片写入笔记", "note_write" in cards.tools, str(cards.tools))
    notes_dir = workspace(data_dir) / "notes" / course["id"]
    notes = sorted(notes_dir.glob("*.md")) if notes_dir.is_dir() else []
    check("笔记落在课程隔离目录", len(notes) > 0, f"{[n.name for n in notes]}")

    # ---- 第 7 步：错题复盘 ----
    print("\n[7] 错题复盘")
    review = ask(base, session["id"], "复盘一下我做错的地方", "j-review")
    check("复盘读了学习档案", "get_archive" in review.tools, str(review.tools))

    # ---- 第 8 步：联网调研与来源标注 ----
    print("\n[8] 联网调研")
    research = ask(base, session["id"], "联网查一下 Linux 现在用的调度器是什么", "j-research")
    check("联网调研用了 web_search", "web_search" in research.tools, str(research.tools))
    check("网络结论明确标注非教材", "不是当前教材结论" in research.answer or "来源" in research.answer,
          research.answer[:120])

    # ---- 第 9 步：课程边界 ----
    print("\n[9] 课程边界")
    cross = ask(base, session["id"], "批量规范化是怎么做的？", "j-cross")
    check("不越界引用别课教材",
          "以下不是当前教材结论" in cross.answer or len(cross.named("citation")) == 0,
          cross.answer[:120])

    general = call(base, "/sessions", {"scope_mode": "general"})
    inferred = ask(base, general["id"], "卷积神经网络的池化层有什么用？", "j-infer")
    check("通用会话按学科解析到深度学习", inferred.course_name == "深度学习", str(inferred.course_name))
    # 认不出课程时按通用知识聊，不再回一句「请说明课程名称」；但也不能凭空取证。
    vague = ask(base, call(base, "/sessions", {"scope_mode": "general"})["id"], "帮我复习一下", "j-vague")
    check("认不出课程也正常作答", vague.finish_reason == "stop" and len(vague.answer) > 10, vague.finish_reason)
    check("含糊提问不乱取证", not vague.named("citation"), f"{len(vague.named('citation'))} 条引用")
    both = ask(base, call(base, "/sessions", {"scope_mode": "general"})["id"],
               "深度学习和操作系统哪个更该先学？", "j-both")
    check("一句话点了两门课就问清楚", both.finish_reason == "course_unresolved", both.finish_reason)

    # ---- 第 10 步：会话改名与压缩 ----
    print("\n[10] 会话管理")
    renamed = call(base, f"/sessions/{session['id']}", {"title": "操作系统冲刺"}, method="PATCH")
    check("会话可改名", renamed["title"] == "操作系统冲刺", str(renamed))
    compactions = db(data_dir).execute("SELECT count(*) c FROM session_compactions").fetchone()["c"]
    # 这一步只报信息不判定：本旅程的历史长度远到不了压缩阈值，
    # 压缩链路由 tests/backend/test_compaction.py 覆盖。
    print(f"  INFO  会话压缩 {compactions} 次（本旅程历史未超阈值，0 属正常）")

    # ---- 第 11 步：可观测 ----
    print("\n[11] 可观测")
    traces = sorted((workspace(data_dir) / "traces").glob("*.jsonl"))
    lines = [json.loads(line) for path in traces for line in path.read_text().splitlines() if line.strip()]
    check("每轮都有 trace", len(lines) >= 10, f"{len(lines)} 条")
    check("trace 记录工具决策", any(t.get("decision") for line in lines for t in line.get("tools", [])))
    check("trace 带提示词版本", all(line.get("prompt_version") for line in lines))

    # ---- 第 12 步：知识页成为第三类可引用来源 ----
    # 放在最后：这一步会给这门课开知识页，前面几步的引用构成就不再是纯教材的了。
    wiki_citation_step(base, course, material["id"], wiki_budget)


# 既要有教材原文答得上的部分（SJF 的周转时间），也要有只有知识页答得上的部分（整体分几块）。
# 只问后者的话教材那一路会空，「教材席位一条不少」就变成两个空列表相等，等于没测。
WIKI_QUESTION = "这门课整体分成哪几部分？SJF 的平均周转时间为什么更短？"


def wiki_citation_step(base: str, course: dict, material_id: str, budget: int) -> None:
    """知识页要能在真实一轮里被引用，而且不能挤掉教材席位。

    此前只验到「知识页进了 chunks 表」。进了表不等于用得上：种子检索的两个名额是固定的，
    引用编号、kind、页码留空这几件事都在对话这一层才发生。
    """
    print("\n[12] 知识页作为第三类可引用来源")

    def fresh_ask(tag: str) -> Turn:
        session = call(base, "/sessions", {"scope_mode": "course", "course_id": course["id"]})["id"]
        return ask(base, session, WIKI_QUESTION, tag)

    before = fresh_ask("j-wiki-before")
    seeded = [citation_key(item) for item in seed_citations(before)]
    baseline = [key for key in seeded if key[0] == "material"]
    # 基线为空的话「教材席位一条不少」就退化成两个空列表相等，那条判据等于没写。
    check("基线里本来就有教材引用", bool(baseline), "种子检索一条都没召回，后面的比对失去意义")
    check("开知识页之前没有知识页引用", baseline == seeded, str(seeded))
    print(f"  INFO  基线种子引用 {len(baseline)} 条")

    call(base, f"/courses/{course['id']}", {"wiki_enabled": True}, method="PATCH")
    estimate = call(base, f"/materials/{material_id}/wiki/estimate")
    print(f"  INFO  预计 {estimate['pages']} 页 · {estimate['calls']} 次模型调用")
    if estimate["calls"] > budget:
        check("知识页构建在预算之内", False, f"预计 {estimate['calls']} 次 > --wiki-budget {budget}")
        return
    built = wait_job(base, call(base, f"/materials/{material_id}/wiki", {})["id"])
    if not check("知识页构建完成", built["status"] == "completed", str(built.get("error"))):
        return
    print(f"  INFO  {built.get('error')}")

    after = fresh_ask("j-wiki-after")
    citations = after.named("citation")
    wiki_hits = [item for item in citations if item.get("kind") == "wiki"]
    print("  INFO  这一轮的引用列表：")
    for item in citations:
        print(f"          [{item['number']}] kind={item['kind']} "
              f"{item.get('document') or item.get('concept_name')} "
              f"page={item.get('page')} {(item.get('snippet') or '')[:40]!r}")
    check("回答里出现了知识页引用", bool(wiki_hits),
          str([(item["kind"], item.get("document") or item.get("concept_name")) for item in citations]))
    check("知识页引用标得出是转述",
          all(item.get("page") is None and item.get("concept_name") for item in wiki_hits), str(wiki_hits[:1]))
    # 正文里标不标它是模型的选择：从教材原文答同一句也是对的，所以只报不判。
    marked = [item["number"] for item in wiki_hits if f"[{item['number']}]" in after.answer]
    print(f"  INFO  这一轮正文里标了知识页编号 {marked or '无'}")

    kept = [citation_key(item) for item in seed_citations(after) if item.get("kind") == "material"]
    check("教材席位一条不少", kept == baseline, f"{baseline} → {kept}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--data-dir", default="testdata/e2e-fresh")
    parser.add_argument("--wiki-budget", type=int, default=20,
                        help="第 12 步给这门课写知识页的模型调用上限，预计超过就不建")
    args = parser.parse_args()
    data_dir = ROOT / args.data_dir

    try:
        journey(args.base, data_dir, wiki_budget=args.wiki_budget)
    except urllib.error.HTTPError as error:
        print(f"\nHTTP 错误：{error.code} {error.read().decode()[:300]}")
        return 2
    except Exception as error:  # noqa: BLE001 - 旅程脚本要把异常也算作失败
        print(f"\n中断：{type(error).__name__} {error}")
        results.append(("旅程未走完", False, str(error)))

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAIL {name} — {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
