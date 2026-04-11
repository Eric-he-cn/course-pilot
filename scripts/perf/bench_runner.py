"""Run internal performance benchmark through OrchestrationRunner main chain."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.metrics import trace_scope  # noqa: E402
from core.orchestration.runner import OrchestrationRunner  # noqa: E402


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            rows.append(json.loads(s))
    return rows


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    rank = (len(xs) - 1) * (p / 100.0)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return xs[lo]
    w = rank - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def _mean(values: Iterable[float]) -> float:
    xs = [float(v) for v in values]
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _sum_key(rows: List[Dict[str, Any]], key: str) -> float:
    total = 0.0
    for row in rows:
        v = row.get(key)
        if isinstance(v, (int, float)):
            total += float(v)
    return total


def _extract_doc_ids(citations: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for c in citations:
        if isinstance(c, dict):
            doc_id = c.get("doc_id")
            if isinstance(doc_id, str) and doc_id:
                out.append(doc_id)
    return out


def _metric_block(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    prompt_tokens = []
    for r in rows:
        p = float(r.get("prompt_tokens_sum", 0.0) or 0.0)
        if p <= 0:
            p = float(r.get("prompt_tokens_est_sum", 0.0) or 0.0)
        prompt_tokens.append(p)
    llm_calls = [float(r.get("llm_call_count", 0.0)) for r in rows]
    llm_ms = [float(r.get("avg_llm_ms", 0.0)) for r in rows]
    first_token = [float(r.get("first_token_latency_ms", 0.0)) for r in rows]
    e2e = [float(r.get("e2e_latency_ms", 0.0)) for r in rows]
    # RAG 相关统计排除离群冷启动样本，避免污染检索性能结论。
    rag_rows = [r for r in rows if float(r.get("retrieval_ms_avg_trace", 0.0) or 0.0) <= 1000.0]
    retrieval_ms = [float(r.get("retrieval_ms_avg_trace", 0.0)) for r in rag_rows]
    has_gold_rows = [r for r in rag_rows if float(r.get("rag_has_gold", 0.0)) > 0]
    hit_vals = [float(r.get("rag_hit", 0.0)) for r in has_gold_rows]
    top1_vals = [float(r.get("rag_top1", 0.0)) for r in has_gold_rows]
    precision_vals = [float(r.get("rag_precision", 0.0)) for r in has_gold_rows]
    tool_call_rate_vals = [1.0 if int(r.get("tool_call_count", 0)) > 0 else 0.0 for r in rows]
    error_vals = [1.0 if r.get("case_error") else 0.0 for r in rows]
    replan_vals = [1.0 if r.get("replan_triggered") else 0.0 for r in rows]
    regen_final_vals = [1.0 if r.get("regen_final") else 0.0 for r in rows]
    duplicate_tool_call_rate_vals = [float(r.get("duplicate_tool_call_rate_case", 0.0) or 0.0) for r in rows]
    fallback_rate_vals = [float(r.get("fallback_rate_case", 0.0) or 0.0) for r in rows]
    mode_override_vals = [float(r.get("resolved_mode_override_count", 0.0) or 0.0) for r in rows]
    session_store_hit_vals = [float(r.get("session_store_hit_rate_case", 0.0) or 0.0) for r in rows]

    tool_calls_total = _sum_key(rows, "tool_call_count")
    tool_success_total = _sum_key(rows, "tool_success_count")
    tool_success_rate = 1.0 if tool_calls_total <= 0 else tool_success_total / tool_calls_total

    return {
        "num_rows": float(len(rows)),
        "avg_prompt_tokens": _mean(prompt_tokens),
        "p50_prompt_tokens": _percentile(prompt_tokens, 50),
        "p95_prompt_tokens": _percentile(prompt_tokens, 95),
        "llm_call_count_avg": _mean(llm_calls),
        "avg_llm_ms": _mean(llm_ms),
        "p50_first_token_latency_ms": _percentile(first_token, 50),
        "p95_first_token_latency_ms": _percentile(first_token, 95),
        "p50_e2e_latency_ms": _percentile(e2e, 50),
        "p95_e2e_latency_ms": _percentile(e2e, 95),
        "avg_retrieval_ms": _mean(retrieval_ms),
        "p95_retrieval_ms": _percentile(retrieval_ms, 95),
        "hit_at_k": _mean(hit_vals),
        "top1_acc": _mean(top1_vals),
        "precision_at_k": _mean(precision_vals),
        "tool_call_rate": _mean(tool_call_rate_vals),
        "tool_success_rate": tool_success_rate,
        "error_rate": _mean(error_vals),
        "replan_trigger_rate": _mean(replan_vals),
        "regen_final_rate": _mean(regen_final_vals),
        "duplicate_tool_call_rate": _mean(duplicate_tool_call_rate_vals),
        "fallback_rate": _mean(fallback_rate_vals),
        "resolved_mode_override_count": _mean(mode_override_vals),
        "session_store_hit_rate": _mean(session_store_hit_vals),
    }


def _write_summary_markdown(path: Path, profile: str, summary: Dict[str, Any]) -> None:
    def fmt(v: Any) -> str:
        if isinstance(v, (int, float)):
            return f"{float(v):.4f}"
        return str(v)

    lines: List[str] = [f"# 性能评估汇总 - {profile}", ""]
    lines.append(f"- 样本数: {int(summary.get('num_rows', 0))}")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---:|")
    keys = [
        "avg_prompt_tokens",
        "p50_prompt_tokens",
        "p95_prompt_tokens",
        "llm_call_count_avg",
        "avg_llm_ms",
        "p50_first_token_latency_ms",
        "p95_first_token_latency_ms",
        "p50_e2e_latency_ms",
        "p95_e2e_latency_ms",
        "avg_retrieval_ms",
        "p95_retrieval_ms",
        "hit_at_k",
        "top1_acc",
        "precision_at_k",
        "tool_call_rate",
        "tool_success_rate",
        "error_rate",
        "replan_trigger_rate",
        "regen_final_rate",
        "duplicate_tool_call_rate",
        "fallback_rate",
        "resolved_mode_override_count",
        "session_store_hit_rate",
    ]
    for key in keys:
        lines.append(f"| `{key}` | {fmt(summary.get(key, 0.0))} |")

    by_mode = summary.get("by_mode", {})
    for mode in ("learn", "practice", "exam"):
        block = by_mode.get(mode)
        if not block:
            continue
        lines.append("")
        lines.append(f"## {mode}")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|---|---:|")
        for key in keys:
            lines.append(f"| `{key}` | {fmt(block.get(key, 0.0))} |")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _case_repeat_key(case_id: str, repeat: int) -> str:
    return f"{case_id}#{repeat}"


def _run_case_once(
    runner: OrchestrationRunner,
    case: Dict[str, Any],
    repeat: int,
    profile: str,
    gold_map: Dict[str, List[str]],
) -> Dict[str, Any]:
    case_id = str(case.get("case_id", "")).strip()
    mode = str(case.get("mode", "learn")).strip()
    course_name = str(case.get("course_name", "")).strip()
    message = str(case.get("message", ""))
    history = case.get("history") or []
    if not isinstance(history, list):
        history = []

    replan_counter = {"n": 0}
    original_replan = runner.router.replan

    def _replan_wrapper(*args, **kwargs):
        replan_counter["n"] += 1
        return original_replan(*args, **kwargs)

    runner.router.replan = _replan_wrapper

    text_parts: List[str] = []
    citations: List[Dict[str, Any]] = []
    first_token_latency_ms: float = 0.0
    case_error = None
    t0 = perf_counter()

    try:
        with trace_scope({"case_id": case_id, "mode": mode, "repeat": repeat}) as trace:
            for chunk in runner.run_stream(
                course_name=course_name,
                mode=mode,
                user_message=message,
                state={},
                history=history,
            ):
                if isinstance(chunk, str):
                    if chunk and first_token_latency_ms <= 0:
                        first_token_latency_ms = (perf_counter() - t0) * 1000.0
                    text_parts.append(chunk)
                elif isinstance(chunk, dict):
                    maybe_citations = chunk.get("__citations__")
                    if isinstance(maybe_citations, list):
                        citations = maybe_citations

            events = list(trace.events)
    except Exception as ex:
        events = []
        case_error = f"{type(ex).__name__}: {ex}"
    finally:
        runner.router.replan = original_replan

    e2e_latency_ms = (perf_counter() - t0) * 1000.0
    response_text = "".join(text_parts)
    response_chars = len(response_text)

    llm_events = [e for e in events if e.get("type") == "llm_call"]
    retrieval_events = [e for e in events if e.get("type") == "retrieval"]
    tool_events = [e for e in events if e.get("type") == "tool_call"]
    tool_dedup_events = [e for e in events if e.get("type") == "tool_dedup"]
    fallback_events = [e for e in events if e.get("type") == "runtime_fallback"]
    mode_override_events = [e for e in events if e.get("type") == "mode_override"]
    taskgraph_events = [e for e in events if e.get("type") == "taskgraph_compiled"]
    session_store_events = [e for e in events if e.get("type") == "session_store_lookup"]

    llm_ms_values = [float(e["llm_ms"]) for e in llm_events if isinstance(e.get("llm_ms"), (int, float))]
    retrieval_ms_values = [
        float(e["retrieval_ms"]) for e in retrieval_events if isinstance(e.get("retrieval_ms"), (int, float))
    ]
    tool_ms_values = [float(e["tool_ms"]) for e in tool_events if isinstance(e.get("tool_ms"), (int, float))]
    first_token_trace_vals = [
        float(e["first_token_latency_ms"])
        for e in llm_events
        if isinstance(e.get("first_token_latency_ms"), (int, float))
    ]

    prompt_token_usage_events = [
        e for e in llm_events if isinstance(e.get("prompt_tokens"), (int, float))
    ]
    completion_token_usage_events = [
        e for e in llm_events if isinstance(e.get("completion_tokens"), (int, float))
    ]
    prompt_tokens_sum = _sum_key(prompt_token_usage_events, "prompt_tokens")
    completion_tokens_sum = _sum_key(completion_token_usage_events, "completion_tokens")
    prompt_tokens_est_sum = _sum_key(llm_events, "prompt_tokens_est")
    prompt_tokens_from_est = False
    prompt_tokens_source = "usage" if prompt_token_usage_events else "estimate"
    # 仅当完全拿不到 provider usage 时，才回退到估算值。
    if not prompt_token_usage_events and prompt_tokens_est_sum > 0:
        prompt_tokens_sum = prompt_tokens_est_sum
        prompt_tokens_from_est = True
    tool_success_count = sum(1 for e in tool_events if e.get("tool_success"))
    dedup_hit_count = sum(1 for e in tool_dedup_events if bool(e.get("dedup_hit")))
    dedup_total_count = len(tool_dedup_events)
    duplicate_tool_call_rate_case = (
        float(dedup_hit_count) / float(dedup_total_count) if dedup_total_count > 0 else 0.0
    )
    regen_final = any(bool(e.get("final_output_regen")) for e in llm_events)
    final_output_source = ""
    for e in llm_events:
        src = e.get("final_output_source")
        if isinstance(src, str) and src:
            final_output_source = src
    replan_triggered = replan_counter["n"] > 0
    fallback_rate_case = 1.0 if fallback_events else 0.0
    resolved_mode_override_count = len(mode_override_events)
    taskgraph_route = ""
    if taskgraph_events:
        taskgraph_route = str(taskgraph_events[-1].get("route", "") or "")
    session_store_hit_rate_case = (
        float(sum(1 for e in session_store_events if bool(e.get("hit")))) / float(len(session_store_events))
        if session_store_events
        else 0.0
    )

    doc_ids = _extract_doc_ids(citations)
    gold_doc_ids = gold_map.get(case_id, [])
    gold_set = set(gold_doc_ids)
    if gold_set:
        rag_has_gold = 1.0
        rag_hit = 1.0 if any(d in gold_set for d in doc_ids) else 0.0
        rag_top1 = 1.0 if doc_ids and doc_ids[0] in gold_set else 0.0
        rag_precision = (
            float(sum(1 for d in doc_ids if d in gold_set)) / float(len(doc_ids)) if doc_ids else 0.0
        )
    else:
        rag_has_gold = 0.0
        rag_hit = 0.0
        rag_top1 = 0.0
        rag_precision = 0.0

    row: Dict[str, Any] = {
        "case_id": case_id,
        "mode": mode,
        "course_name": course_name,
        "message": message,
        "history_len": len(history),
        "first_token_latency_ms": float(first_token_latency_ms),
        "e2e_latency_ms": float(e2e_latency_ms),
        "response_chars": response_chars,
        "citations": citations,
        "case_error": case_error,
        "llm_call_count": len(llm_events),
        "prompt_tokens_sum": float(prompt_tokens_sum),
        "completion_tokens_sum": float(completion_tokens_sum),
        "prompt_tokens_est_sum": float(prompt_tokens_est_sum),
        "prompt_tokens_from_est": prompt_tokens_from_est,
        "prompt_tokens_source": prompt_tokens_source,
        "avg_llm_ms": _mean(llm_ms_values),
        "first_token_latency_ms_trace": _mean(first_token_trace_vals),
        "retrieval_call_count": len(retrieval_events),
        "retrieval_ms_avg_trace": _mean(retrieval_ms_values),
        "retrieval_ms_values": retrieval_ms_values,
        "tool_call_count": len(tool_events),
        "tool_success_count": tool_success_count,
        "tool_elapsed_ms_avg_trace": _mean(tool_ms_values),
        "tool_dedup_event_count": dedup_total_count,
        "tool_dedup_hit_count": dedup_hit_count,
        "duplicate_tool_call_rate_case": duplicate_tool_call_rate_case,
        "fallback_rate_case": fallback_rate_case,
        "resolved_mode_override_count": resolved_mode_override_count,
        "taskgraph_route": taskgraph_route,
        "session_store_hit_rate_case": session_store_hit_rate_case,
        "regen_final": regen_final,
        "final_output_source": final_output_source,
        "replan_triggered": replan_triggered,
        "trace_status": "error" if case_error else "ok",
        "rag_hit": rag_hit,
        "rag_top1": rag_top1,
        "rag_precision": rag_precision,
        "rag_has_gold": rag_has_gold,
        "profile": profile,
        "repeat": repeat,
    }
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark runner for course-pilot baseline/after profiles.")
    parser.add_argument("--cases", default=str(ROOT / "benchmarks" / "cases_v1.jsonl"))
    parser.add_argument("--gold", default=str(ROOT / "benchmarks" / "rag_gold_v1.jsonl"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "baseline_v1"))
    parser.add_argument("--profile", default="baseline_v1")
    parser.add_argument("--repeats", type=int, default=2)
    args = parser.parse_args()

    cases_path = Path(args.cases)
    gold_path = Path(args.gold)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_path = out_dir / "baseline_raw.jsonl"
    summary_json_path = out_dir / "baseline_summary.json"
    summary_md_path = out_dir / "baseline_summary.md"
    checkpoint_path = out_dir / "baseline_checkpoint.json"

    cases = _load_jsonl(cases_path)
    if not cases:
        print(f"[bench] no cases found: {cases_path}")
        return 1

    gold_rows = _load_jsonl(gold_path)
    gold_map: Dict[str, List[str]] = {}
    for row in gold_rows:
        cid = str(row.get("case_id", "")).strip()
        g = row.get("gold_doc_ids") or []
        if cid:
            gold_map[cid] = [str(x) for x in g if isinstance(x, str)]

    existing_rows = _load_jsonl(raw_path)
    done_keys = {_case_repeat_key(str(r.get("case_id", "")), int(r.get("repeat", 0))) for r in existing_rows}

    runner = OrchestrationRunner()
    total = len(cases) * max(1, int(args.repeats))
    completed = len(done_keys)

    print(f"[bench] profile={args.profile} cases={len(cases)} repeats={args.repeats} total={total}")
    print(f"[bench] output={out_dir}")

    try:
        for case in cases:
            case_id = str(case.get("case_id", "")).strip()
            for repeat in range(1, int(args.repeats) + 1):
                key = _case_repeat_key(case_id, repeat)
                if key in done_keys:
                    continue
                row = _run_case_once(
                    runner=runner,
                    case=case,
                    repeat=repeat,
                    profile=args.profile,
                    gold_map=gold_map,
                )
                _append_jsonl(raw_path, row)
                existing_rows.append(row)
                done_keys.add(key)
                completed += 1

                checkpoint = {
                    "profile": args.profile,
                    "completed": completed,
                    "total": total,
                    "done": completed >= total,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
                print(
                    f"[bench] {completed}/{total} "
                    f"{row['case_id']}#r{row['repeat']} "
                    f"e2e={row['e2e_latency_ms']:.1f}ms err={int(bool(row['case_error']))}"
                )
    except KeyboardInterrupt:
        print("\n[bench] interrupted by user")

    summary = _metric_block(existing_rows)
    summary["profile"] = args.profile
    summary["num_rows"] = len(existing_rows)
    by_mode = {}
    for mode in ("learn", "practice", "exam"):
        mode_rows = [r for r in existing_rows if str(r.get("mode")) == mode]
        if mode_rows:
            by_mode[mode] = _metric_block(mode_rows)
            by_mode[mode]["num_rows"] = len(mode_rows)
    summary["by_mode"] = by_mode

    summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_summary_markdown(summary_md_path, args.profile, summary)

    checkpoint = {
        "profile": args.profile,
        "completed": len(done_keys),
        "total": total,
        "done": len(done_keys) >= total,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[bench] done={checkpoint['done']} rows={len(existing_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
