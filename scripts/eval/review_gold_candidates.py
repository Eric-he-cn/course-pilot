"""Interactive review CLI for promoting judged gold candidates into active benchmark files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval.gold_pipeline_utils import (  # noqa: E402
    BENCHMARK_DIR,
    append_jsonl,
    build_official_case_row,
    build_official_gold_row,
    ensure_pipeline_files,
    load_jsonl,
    now_iso,
    parse_citation_indexes,
    upsert_jsonl_by_case_id,
    write_jsonl,
)


def _print_candidate(row: Dict[str, Any]) -> None:
    judge = row.get("judge") if isinstance(row.get("judge"), dict) else {}
    plan_summary = row.get("plan_summary") if isinstance(row.get("plan_summary"), dict) else {}
    trace_summary = row.get("trace_summary") if isinstance(row.get("trace_summary"), dict) else {}
    print("\n" + "=" * 88)
    print(f"case_id: {row.get('case_id', '')}")
    print(f"course:   {row.get('course_name', '')}")
    print(f"question: {row.get('message', '')}")
    print(f"score:    {judge.get('overall_score', 0.0):.2f} | decision={judge.get('decision', '')} | confidence={judge.get('confidence', 0.0):.2f}")
    print(f"reason:   {judge.get('reasoning', '')}")
    print(f"route:    {plan_summary.get('resolved_mode', '')} / {plan_summary.get('workflow_template', '')} / {plan_summary.get('action_kind', '')}")
    print(f"trace:    route={trace_summary.get('taskgraph_route', '')} fallback={trace_summary.get('fallback', False)} e2e_ms={trace_summary.get('e2e_ms', 0.0):.1f}")
    print("\n回答：")
    print(str(row.get("response_text", "") or "")[:1200])
    print("\n引用：")
    selected = set(int(idx) for idx in row.get("selected_citation_indexes", []) if isinstance(idx, int))
    citations = row.get("citations") if isinstance(row.get("citations"), list) else []
    for idx, citation in enumerate(citations):
        if not isinstance(citation, dict):
            continue
        marker = "*" if idx in selected else " "
        print(
            f"[{idx}]{marker} doc={citation.get('doc_id', '')} page={citation.get('page', '')} "
            f"chunk={citation.get('chunk_id', '')} preview={str(citation.get('text_preview', '') or citation.get('text', '') or '')[:180]}"
        )


def _resolve_accept_indexes(row: Dict[str, Any]) -> List[int]:
    citations = row.get("citations") if isinstance(row.get("citations"), list) else []
    suggested = [idx for idx in row.get("selected_citation_indexes", []) if isinstance(idx, int)]
    default_text = ",".join(str(idx) for idx in suggested)
    while True:
        raw = input(f"确认用于正式 gold 的 citation 索引（默认 {default_text or '空'}）: ").strip()
        indexes = parse_citation_indexes(raw, len(citations)) if raw else suggested
        if indexes:
            return indexes
        print("至少需要一个 citation 索引才能正式入库。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Review gold candidates and promote them into active benchmark files.")
    parser.add_argument("--bench-dir", default=str(BENCHMARK_DIR))
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of candidates to review in one run.")
    args = parser.parse_args()

    bench_dir = Path(args.bench_dir)
    ensure_pipeline_files(bench_dir)
    candidates_path = bench_dir / "gold_candidates.jsonl"
    manual_fix_path = bench_dir / "gold_manual_fix.jsonl"
    rejected_path = bench_dir / "gold_rejected.jsonl"
    sessions_path = bench_dir / "gold_label_sessions.jsonl"
    cases_path = bench_dir / "cases_v1.jsonl"
    gold_path = bench_dir / "rag_gold_v1.jsonl"

    rows = load_jsonl(candidates_path)
    if not rows:
        print("没有待复查的 gold 候选。")
        return 0

    remaining: List[Dict[str, Any]] = []
    reviewed = 0
    stop = False
    for row in rows:
        if stop:
            remaining.append(row)
            continue
        if args.limit > 0 and reviewed >= int(args.limit):
            remaining.append(row)
            continue
        _print_candidate(row)
        action = input("\n操作: [a]ccept [m]anual_fix [r]eject [s]kip [q]uit: ").strip().lower()
        if action == "q":
            remaining.append(row)
            stop = True
            continue
        if action == "s":
            remaining.append(row)
            continue
        reviewed_at = now_iso()
        if action == "a":
            indexes = _resolve_accept_indexes(row)
            case_row = build_official_case_row(row)
            gold_row = build_official_gold_row(row, selected_indexes=indexes, verified_at=reviewed_at)
            upsert_jsonl_by_case_id(cases_path, case_row)
            upsert_jsonl_by_case_id(gold_path, gold_row)
            append_jsonl(
                sessions_path,
                {
                    **row,
                    "event": "review_accept",
                    "reviewed_at": reviewed_at,
                    "review_selected_citation_indexes": indexes,
                },
            )
            print(f"已写入正式 benchmark: {row.get('case_id', '')}")
            reviewed += 1
            continue
        if action == "m":
            note = input("manual_fix 备注（可空）: ").strip()
            upsert_jsonl_by_case_id(
                manual_fix_path,
                {
                    **row,
                    "manual_fix_note": note,
                    "reviewed_at": reviewed_at,
                    "review_decision": "manual_fix",
                },
            )
            append_jsonl(
                sessions_path,
                {
                    **row,
                    "event": "review_manual_fix",
                    "manual_fix_note": note,
                    "reviewed_at": reviewed_at,
                },
            )
            reviewed += 1
            continue
        if action == "r":
            reason = input("reject 原因（可空）: ").strip()
            upsert_jsonl_by_case_id(
                rejected_path,
                {
                    **row,
                    "reject_reason": reason,
                    "reviewed_at": reviewed_at,
                    "review_decision": "reject",
                },
            )
            append_jsonl(
                sessions_path,
                {
                    **row,
                    "event": "review_reject",
                    "reject_reason": reason,
                    "reviewed_at": reviewed_at,
                },
            )
            reviewed += 1
            continue
        print("输入无效，保留该候选待下次处理。")
        remaining.append(row)

    write_jsonl(candidates_path, remaining)
    print(f"\n已复查 {reviewed} 条，待处理候选剩余 {len(remaining)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
