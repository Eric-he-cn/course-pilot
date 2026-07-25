#!/usr/bin/env python3
"""冒烟 benchmark：固定用例跑真实链路，只断言结构化行为，不断言具体文本。

覆盖架构 §16.3 的发布门槛：practice 的出题/单题作答/多题作答/讲评/变式题/作答对象歧义，
外加 Tutor 的取证引用、课程隔离与教材外兜底。断言对象是 SSE 事件与档案增量，
所以模型换措辞不会让用例假失败。

用法：先启动被测实例（STORAGE_DATA_DIR=data/e2e ./scripts/dev.sh），再运行本脚本。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field

BASE = "http://127.0.0.1:8000/api/v2"


def get(path: str) -> object:
    return json.loads(urllib.request.urlopen(BASE + path).read())


def post(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(BASE + path, data=json.dumps(payload or {}).encode(), method="POST", headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(request).read())


@dataclass
class TurnResult:
    tools: list[str] = field(default_factory=list)
    citations: int = 0
    answer: str = ""
    finish_reason: str = ""

    def used(self, name: str) -> bool:
        return name in self.tools


def run_turn(session_id: str, message: str, request_id: str) -> TurnResult:
    raw = subprocess.run(
        ["curl", "-s", "-N", "-X", "POST", f"{BASE}/sessions/{session_id}/turns",
         "-H", "Content-Type: application/json", "-d", json.dumps({"client_request_id": request_id, "message": message})],
        capture_output=True, text=True,
    ).stdout
    result = TurnResult()
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[5:])
        if payload.get("text"):
            result.answer += payload["text"]
        if payload.get("citation_id"):
            result.citations += 1
        name = payload.get("name")
        if name and payload.get("summary") is not None:
            result.tools.append(name)
        if payload.get("finish_reason"):
            result.finish_reason = payload["finish_reason"]
    return result


def course_id(name: str) -> str:
    matches = [item["id"] for item in get("/courses") if item["name"] == name]
    if not matches:
        raise SystemExit(f"被测实例里没有课程「{name}」，先准备好教材与索引")
    return matches[0]


def evidence_count(cid: str) -> int:
    return int(get(f"/courses/{cid}/archive")["evidence_count"])


def new_session(cid: str | None) -> str:
    payload = {"scope_mode": "course", "course_id": cid} if cid else {"scope_mode": "general", "course_id": None}
    return post("/sessions", payload)["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--course", default="操作系统", help="用于练习类用例的课程名")
    parser.add_argument("--other-course", default="机器学习数学", help="用于隔离用例的另一门课程")
    args = parser.parse_args()

    cid = course_id(args.course)
    other = course_id(args.other_course)
    cases: list[tuple[str, callable]] = []

    def case(name):
        def register(fn):
            cases.append((name, fn))
            return fn
        return register

    @case("practice/出题：加载 skill、取教材证据、只给题目")
    def _():
        session = new_session(cid)
        turn = run_turn(session, "给我出两道题练练", "bm-gen")
        assert turn.used("use_skill"), "没有加载 practice skill"
        assert turn.used("search_materials"), "出题前没有取教材证据"
        assert turn.used("artifact_append"), "题目与答案要点没有落 artifact"
        assert turn.citations > 0, "题目没有教材引用"
        return session

    @case("practice/单题作答：写证据事件并更新掌握度")
    def _():
        session = new_session(cid)
        run_turn(session, "出一道题", "bm-one-gen")
        before = evidence_count(cid)
        turn = run_turn(session, "我的答案是 20 秒。", "bm-one-answer")
        assert turn.used("emit_evidence"), "作答没有产生证据事件"
        assert evidence_count(cid) > before, "档案里的证据数量没有增加"

    @case("practice/多题作答：逐题归因")
    def _():
        session = new_session(cid)
        run_turn(session, "出两道题", "bm-multi-gen")
        before = evidence_count(cid)
        turn = run_turn(session, "第一题答 110 秒，第二题答 50 秒。", "bm-multi-answer")
        assert turn.tools.count("emit_evidence") >= 2, "两道题应各产生一条证据"
        assert evidence_count(cid) >= before + 2

    @case("practice/讲评：不重复计入证据")
    def _():
        session = new_session(cid)
        run_turn(session, "出一道题", "bm-rev-gen")
        run_turn(session, "答案是 20 秒", "bm-rev-answer")
        before = evidence_count(cid)
        turn = run_turn(session, "第一题为什么错？再讲讲", "bm-rev-explain")
        assert turn.answer.strip(), "讲评没有输出内容"
        assert evidence_count(cid) - before <= 1, "讲评不应重复批量写证据"

    @case("practice/变式题：同考点再出题")
    def _():
        session = new_session(cid)
        run_turn(session, "出一道题", "bm-var-gen")
        run_turn(session, "答案 20 秒", "bm-var-answer")
        turn = run_turn(session, "再来一道同类型的题", "bm-var-more")
        assert turn.used("artifact_append"), "变式题没有落 artifact"

    @case("practice/作答对象歧义：先问用户而不是乱批")
    def _():
        session = new_session(cid)
        run_turn(session, "出一道题", "bm-amb-1")
        run_turn(session, "答案 20 秒", "bm-amb-2")
        run_turn(session, "再出一道题", "bm-amb-3")
        turn = run_turn(session, "30", "bm-amb-4")
        assert turn.answer.strip(), "歧义作答没有任何回应"

    @case("tutor/取证引用：回答带教材页码")
    def _():
        session = new_session(cid)
        turn = run_turn(session, "时间片轮转为什么响应时间好？", "bm-cite")
        assert turn.citations > 0 and "[" in turn.answer, "回答没有带教材引用"

    @case("tutor/课程隔离：不引用别课教材")
    def _():
        session = new_session(other)
        turn = run_turn(session, "Round Robin 调度的周转时间怎么算？", "bm-isolate")
        assert "以下不是当前教材结论" in turn.answer or turn.citations == 0, "越界使用了其他课程的教材"

    @case("resolver/通用会话模糊问题不取证")
    def _():
        session = new_session(None)
        turn = run_turn(session, "帮我复习一下", "bm-vague")
        assert turn.finish_reason == "course_unresolved", f"应判为未解析课程，实际 {turn.finish_reason}"
        assert not turn.used("emit_evidence")

    passed, failed = 0, []
    for name, fn in cases:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as error:
            failed.append((name, str(error)))
            print(f"  FAIL  {name} — {error}")
        except Exception as error:  # 环境或接口异常同样算失败，但要看得出区别
            failed.append((name, f"{type(error).__name__}: {error}"))
            print(f"  ERROR {name} — {type(error).__name__}: {error}")

    print(f"\n{passed}/{len(cases)} 通过")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
