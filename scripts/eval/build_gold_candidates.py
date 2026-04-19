"""Collect real learn-mode samples and first-screen them into gold candidate buckets."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from core.metrics import trace_scope  # noqa: E402
from core.orchestration.runner import OrchestrationRunner  # noqa: E402
from scripts.eval.gold_pipeline_utils import (  # noqa: E402
    BENCHMARK_DIR,
    DEFAULT_DATA_DIR,
    DEFAULT_INCLUDED_COURSES,
    append_jsonl,
    build_case_id,
    citations_to_dicts,
    ensure_pipeline_files,
    extract_internal_meta,
    generate_question_suggestions,
    load_processed_case_ids,
    now_iso,
    plan_summary,
    plan_to_dict,
    scan_indexed_courses,
    summarize_trace_events,
)
from scripts.eval.gold_screen_judge import (  # noqa: E402
    DEFAULT_THRESHOLD,
    build_client_from_env,
    judge_gold_sample,
)


def _print_suggestions(suggestions: List[Dict[str, Any]]) -> None:
    if not suggestions:
        print("没有可用建议题目。")
        return
    print("\n建议题目：")
    for idx, item in enumerate(suggestions, start=1):
        print(f"[{idx:02d}] {item['course_name']} | {item['message']}")


def _choose_course(courses: List[str]) -> str:
    if len(courses) == 1:
        return courses[0]
    print("\n可用课程：")
    for idx, course_name in enumerate(courses, start=1):
        print(f"[{idx}] {course_name}")
    while True:
        raw = input("选择课程编号: ").strip()
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(courses):
                return courses[index]
        print("输入无效，请重新输入。")


def _collect_targets(
    *,
    courses: List[str],
    suggestions: List[Dict[str, Any]],
    run_all: bool,
    manual_question: Optional[str],
    course_name: Optional[str],
) -> List[Dict[str, Any]]:
    if manual_question:
        resolved_course = str(course_name or "").strip() or _choose_course(courses)
        return [{"course_name": resolved_course, "message": manual_question}]
    if run_all:
        return list(suggestions)
    targets: List[Dict[str, Any]] = []
    while True:
        _print_suggestions(suggestions)
        raw = input("\n输入题号、all、m(手动输入) 或 q 退出: ").strip().lower()
        if raw == "q":
            break
        if raw == "all":
            targets.extend(suggestions)
            break
        if raw == "m":
            resolved_course = _choose_course(courses)
            question = input("输入问题: ").strip()
            if question:
                targets.append({"course_name": resolved_course, "message": question})
            continue
        if raw.isdigit():
            index = int(raw) - 1
            if 0 <= index < len(suggestions):
                targets.append(dict(suggestions[index]))
                continue
        print("输入无效，请重新选择。")
    return targets


def _sample_one(
    *,
    runner: OrchestrationRunner,
    course_name: str,
    message: str,
    client,
    judge_model: str,
    threshold: float,
    temperature: float,
    timeout_ms: int,
    bench_dir: Path,
) -> Dict[str, Any]:
    case_id = build_case_id(course_name, message)
    append_jsonl(
        bench_dir / "gold_label_sessions.jsonl",
        {
            "event": "sample_started",
            "case_id": case_id,
            "mode": "learn",
            "course_name": course_name,
            "message": message,
            "started_at": now_iso(),
        },
    )
    replan_counter = {"n": 0}
    original_replan = runner.router.replan

    def _replan_wrapper(*args, **kwargs):
        replan_counter["n"] += 1
        return original_replan(*args, **kwargs)

    runner.router.replan = _replan_wrapper
    response_text = ""
    response = None
    plan = None
    events: List[Dict[str, Any]] = []
    case_error = ""
    t0 = perf_counter()
    try:
        with trace_scope({"case_id": case_id, "mode": "learn", "gold_screen": True}) as trace:
            response, plan = runner.run(
                course_name=course_name,
                mode="learn",
                user_message=message,
                state={},
                history=[],
            )
            response_text = str(getattr(response, "content", "") or "")
            events = list(trace.events)
    except Exception as ex:
        case_error = f"{type(ex).__name__}: {ex}"
    finally:
        runner.router.replan = original_replan
    e2e_latency_ms = (perf_counter() - t0) * 1000.0
    citations = citations_to_dicts(getattr(response, "citations", []) or [])
    tool_calls = list(getattr(response, "tool_calls", []) or []) if response is not None else []
    trace_summary = summarize_trace_events(events, e2e_latency_ms=e2e_latency_ms, replan_triggered=replan_counter["n"] > 0)
    trace_summary["retrieval_empty"] = bool(trace_summary.get("retrieval_empty")) or not bool(citations)
    sample_row: Dict[str, Any] = {
        "case_id": case_id,
        "mode": "learn",
        "course_name": course_name,
        "message": message,
        "history": [],
        "need_rag": True,
        "requires_citations": True,
        "response_text": response_text,
        "reference_answer": response_text,
        "plan": plan_to_dict(plan),
        "plan_summary": plan_summary(plan),
        "citations": citations,
        "session_state": extract_internal_meta(tool_calls, "session_state"),
        "history_summary_state": extract_internal_meta(tool_calls, "history_summary_state"),
        "tool_calls": tool_calls,
        "trace_summary": trace_summary,
        "case_error": case_error,
        "generated_at": now_iso(),
    }
    judge_result = judge_gold_sample(
        payload=sample_row,
        client=client,
        model=judge_model,
        threshold=threshold,
        temperature=temperature,
        timeout_ms=timeout_ms,
    )
    sample_row["judge"] = judge_result
    sample_row["selected_citation_indexes"] = list(judge_result.get("selected_citation_indexes") or [])
    decision = str(judge_result.get("decision", "reject") or "reject")
    bucket_map = {
        "candidate": bench_dir / "gold_candidates.jsonl",
        "manual_fix": bench_dir / "gold_manual_fix.jsonl",
        "reject": bench_dir / "gold_rejected.jsonl",
    }
    target_path = bucket_map.get(decision, bench_dir / "gold_rejected.jsonl")
    append_jsonl(target_path, sample_row)
    append_jsonl(
        bench_dir / "gold_label_sessions.jsonl",
        {
            "event": "sampled",
            "decision": decision,
            "judge_overall_score": judge_result.get("overall_score"),
            "judge_confidence": judge_result.get("confidence"),
            "target_file": str(target_path.relative_to(ROOT)),
            **sample_row,
        },
    )
    print(
        f"[{course_name}] decision={decision} score={judge_result.get('overall_score', 0.0):.2f} "
        f"citations={len(citations)} case_id={case_id}"
    )
    return sample_row


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real learn-mode samples and build gold candidate buckets.")
    parser.add_argument("--bench-dir", default=str(BENCHMARK_DIR))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--course")
    parser.add_argument("--question", action="append", help="Manual question. Can be passed multiple times.")
    parser.add_argument("--count", type=int, default=30, help="Number of suggested questions to generate.")
    parser.add_argument("--run-all-suggestions", action="store_true")
    parser.add_argument("--force", action="store_true", help="Do not skip already processed case_ids.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout-ms", type=int, default=120000)
    args = parser.parse_args()

    bench_dir = Path(args.bench_dir)
    ensure_pipeline_files(bench_dir)
    indexed_courses = scan_indexed_courses(
        args.data_dir,
        included_courses=None if args.course else DEFAULT_INCLUDED_COURSES,
    )
    if args.course:
        indexed_courses = scan_indexed_courses(args.data_dir, included_courses=[str(args.course).strip()])
    if not indexed_courses:
        print("未找到可用课程索引，请先构建工作区索引。")
        return 1

    print("已发现可用课程：")
    for course_name in indexed_courses:
        print(f"- {course_name}")

    suggestions = generate_question_suggestions(indexed_courses, total=max(1, int(args.count)))
    targets: List[Dict[str, Any]] = []
    if args.question:
        resolved_course = str(args.course or "").strip()
        if len(args.question) > 1 and not resolved_course:
            print("传入多个 --question 时请同时指定 --course。")
            return 1
        for question in args.question:
            targets.append({"course_name": resolved_course or _choose_course(indexed_courses), "message": question})
    else:
        targets = _collect_targets(
            courses=indexed_courses,
            suggestions=suggestions,
            run_all=bool(args.run_all_suggestions),
            manual_question=None,
            course_name=args.course,
        )
    if not targets:
        print("未选择任何样本。")
        return 0

    client, cfg = build_client_from_env()
    runner = OrchestrationRunner(data_dir=str(args.data_dir))
    processed_case_ids = set()
    if not args.force:
        processed_case_ids = load_processed_case_ids(bench_dir)
    for item in targets:
        course_name = str(item.get("course_name", "") or "").strip()
        message = str(item.get("message", "") or "").strip()
        if not course_name or not message:
            continue
        case_id = build_case_id(course_name, message)
        if case_id in processed_case_ids:
            print(f"[skip] case_id={case_id} 已存在于候选池/待修池/拒绝池/正式集")
            continue
        _sample_one(
            runner=runner,
            course_name=course_name,
            message=message,
            client=client,
            judge_model=cfg["model"],
            threshold=float(args.threshold),
            temperature=float(args.temperature),
            timeout_ms=max(1000, int(args.timeout_ms)),
            bench_dir=bench_dir,
        )
        processed_case_ids.add(case_id)
    print("\n完成。已写入候选池 / 待修 / 拒绝 / 审计日志。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
