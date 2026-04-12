"""Merge benchmark and judge outputs into a dynamic review report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
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


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _by_case_id(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id", "")).strip()
        if case_id:
            out[case_id] = row
    return out


def _is_case_regression(
    *,
    candidate_row: Dict[str, Any],
    candidate_judge: Dict[str, Any],
    baseline_row: Dict[str, Any] | None,
    baseline_judge: Dict[str, Any] | None,
) -> tuple[bool, List[str]]:
    reasons: List[str] = []
    if candidate_row.get("trace_contract_error"):
        reasons.append("trace_contract_error")
    if float(candidate_row.get("fallback_rate_case", 0.0) or 0.0) > 0:
        reasons.append("runtime_fallback")
    if candidate_row.get("case_error"):
        reasons.append("case_error")
    if candidate_judge and not candidate_judge.get("judge_skipped"):
        if float(candidate_judge.get("overall_score", 0.0) or 0.0) < 0.60:
            reasons.append("low_judge_score")
        if str(candidate_judge.get("pairwise_winner", "") or "") == "baseline":
            reasons.append("pairwise_baseline_wins")
    if float(candidate_row.get("latency_budget_met_case", 1.0) or 1.0) < 1.0:
        reasons.append("latency_budget_exceeded")
    if baseline_row:
        if float(candidate_row.get("rag_hit", 0.0) or 0.0) < float(baseline_row.get("rag_hit", 0.0) or 0.0):
            reasons.append("rag_hit_regressed")
        base_e2e = float(baseline_row.get("e2e_latency_ms", 0.0) or 0.0)
        cand_e2e = float(candidate_row.get("e2e_latency_ms", 0.0) or 0.0)
        if base_e2e > 0 and cand_e2e > base_e2e * 1.2:
            reasons.append("e2e_latency_regressed")
    if baseline_judge and candidate_judge and not baseline_judge.get("judge_skipped") and not candidate_judge.get("judge_skipped"):
        base_score = float(baseline_judge.get("overall_score", 0.0) or 0.0)
        cand_score = float(candidate_judge.get("overall_score", 0.0) or 0.0)
        if cand_score + 0.15 < base_score:
            reasons.append("judge_score_regressed")
    return bool(reasons), reasons


def build_review_report(
    *,
    benchmark_summary: Dict[str, Any],
    benchmark_rows: List[Dict[str, Any]],
    judge_summary: Dict[str, Any],
    judge_rows: List[Dict[str, Any]],
    baseline_benchmark_summary: Dict[str, Any] | None = None,
    baseline_benchmark_rows: List[Dict[str, Any]] | None = None,
    baseline_judge_summary: Dict[str, Any] | None = None,
    baseline_judge_rows: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    baseline_benchmark_rows = baseline_benchmark_rows or []
    baseline_judge_rows = baseline_judge_rows or []
    candidate_by_case = _by_case_id(benchmark_rows)
    candidate_judge_by_case = _by_case_id(judge_rows)
    baseline_by_case = _by_case_id(baseline_benchmark_rows)
    baseline_judge_by_case = _by_case_id(baseline_judge_rows)

    regression_cases: List[Dict[str, Any]] = []
    human_review_queue: List[Dict[str, Any]] = []
    for case_id, candidate_row in candidate_by_case.items():
        candidate_judge = candidate_judge_by_case.get(case_id, {})
        baseline_row = baseline_by_case.get(case_id)
        baseline_judge = baseline_judge_by_case.get(case_id)
        is_regression, reasons = _is_case_regression(
            candidate_row=candidate_row,
            candidate_judge=candidate_judge,
            baseline_row=baseline_row,
            baseline_judge=baseline_judge,
        )
        review_item = {
            "case_id": case_id,
            "mode": candidate_row.get("mode", ""),
            "reasons": reasons,
            "candidate_e2e_ms": candidate_row.get("e2e_latency_ms"),
            "candidate_rag_hit": candidate_row.get("rag_hit"),
            "candidate_judge_score": candidate_judge.get("overall_score"),
            "candidate_judge_confidence": candidate_judge.get("confidence"),
            "pairwise_winner": candidate_judge.get("pairwise_winner", ""),
            "trace_contract_error": candidate_row.get("trace_contract_error", False),
            "fallback_rate_case": candidate_row.get("fallback_rate_case", 0.0),
        }
        if baseline_row:
            review_item["baseline_e2e_ms"] = baseline_row.get("e2e_latency_ms")
            review_item["baseline_rag_hit"] = baseline_row.get("rag_hit")
        if baseline_judge:
            review_item["baseline_judge_score"] = baseline_judge.get("overall_score")
        if is_regression:
            regression_cases.append({**review_item, "review_regression": True})
        if (
            is_regression
            or float(candidate_row.get("fallback_rate_case", 0.0) or 0.0) > 0
            or candidate_row.get("trace_contract_error")
            or float(candidate_judge.get("confidence", 1.0) or 1.0) < 0.55
            or str(candidate_judge.get("label", "") or "") in {"fail", "warn"}
        ):
            human_review_queue.append(review_item)

    candidate_e2e = float(benchmark_summary.get("p50_e2e_latency_ms", 0.0) or 0.0)
    baseline_e2e = float((baseline_benchmark_summary or {}).get("p50_e2e_latency_ms", 0.0) or 0.0)
    candidate_judge_score = float(judge_summary.get("avg_overall_score", 0.0) or 0.0)
    baseline_judge_score = float((baseline_judge_summary or {}).get("avg_overall_score", 0.0) or 0.0)
    report = {
        "candidate_summary": {
            "benchmark": benchmark_summary,
            "judge": judge_summary,
        },
        "baseline_summary": {
            "benchmark": baseline_benchmark_summary or {},
            "judge": baseline_judge_summary or {},
        },
        "headline": {
            "num_cases": len(candidate_by_case),
            "regression_case_count": len(regression_cases),
            "human_review_queue_count": len(human_review_queue),
            "trace_contract_error_count": sum(1 for row in benchmark_rows if row.get("trace_contract_error")),
            "fallback_case_count": sum(1 for row in benchmark_rows if float(row.get("fallback_rate_case", 0.0) or 0.0) > 0),
            "candidate_p50_e2e_latency_ms": candidate_e2e,
            "baseline_p50_e2e_latency_ms": baseline_e2e,
            "candidate_avg_judge_score": candidate_judge_score,
            "baseline_avg_judge_score": baseline_judge_score,
            "delta_p50_e2e_latency_ms": candidate_e2e - baseline_e2e,
            "delta_avg_judge_score": candidate_judge_score - baseline_judge_score,
        },
        "regression_cases": regression_cases,
        "human_review_queue": human_review_queue,
    }
    return report


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_markdown(path: Path, report: Dict[str, Any]) -> None:
    headline = report.get("headline", {})
    lines = [
        "# Dynamic Review Summary",
        "",
        f"- num_cases: {headline.get('num_cases', 0)}",
        f"- regression_case_count: {headline.get('regression_case_count', 0)}",
        f"- human_review_queue_count: {headline.get('human_review_queue_count', 0)}",
        f"- trace_contract_error_count: {headline.get('trace_contract_error_count', 0)}",
        f"- fallback_case_count: {headline.get('fallback_case_count', 0)}",
        f"- candidate_p50_e2e_latency_ms: {float(headline.get('candidate_p50_e2e_latency_ms', 0.0)):.2f}",
        f"- baseline_p50_e2e_latency_ms: {float(headline.get('baseline_p50_e2e_latency_ms', 0.0)):.2f}",
        f"- candidate_avg_judge_score: {float(headline.get('candidate_avg_judge_score', 0.0)):.4f}",
        f"- baseline_avg_judge_score: {float(headline.get('baseline_avg_judge_score', 0.0)):.4f}",
        "",
        "## Regression Cases",
        "",
    ]
    for row in report.get("regression_cases", [])[:20]:
        lines.append(f"- {row.get('case_id', '')}: {', '.join(row.get('reasons', []) or [])}")
    lines.extend(["", "## Human Review Queue", ""])
    for row in report.get("human_review_queue", [])[:20]:
        lines.append(f"- {row.get('case_id', '')}: {', '.join(row.get('reasons', []) or ['review'])}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a dynamic review report from benchmark and judge outputs.")
    parser.add_argument("--benchmark-summary", default=str(ROOT / "data" / "perf_runs" / "baseline_v1" / "baseline_summary.json"))
    parser.add_argument("--benchmark-raw", default=str(ROOT / "data" / "perf_runs" / "baseline_v1" / "baseline_raw.jsonl"))
    parser.add_argument("--judge-summary", default="")
    parser.add_argument("--judge-raw", default="")
    parser.add_argument("--baseline-benchmark-summary", default="")
    parser.add_argument("--baseline-benchmark-raw", default="")
    parser.add_argument("--baseline-judge-summary", default="")
    parser.add_argument("--baseline-judge-raw", default="")
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "review_default"))
    args = parser.parse_args()

    benchmark_summary = _load_json(Path(args.benchmark_summary))
    benchmark_rows = load_jsonl(Path(args.benchmark_raw))
    judge_summary = _load_json(Path(args.judge_summary)) if args.judge_summary else {}
    judge_rows = load_jsonl(Path(args.judge_raw)) if args.judge_raw else []
    report = build_review_report(
        benchmark_summary=benchmark_summary,
        benchmark_rows=benchmark_rows,
        judge_summary=judge_summary,
        judge_rows=judge_rows,
        baseline_benchmark_summary=_load_json(Path(args.baseline_benchmark_summary)) if args.baseline_benchmark_summary else {},
        baseline_benchmark_rows=load_jsonl(Path(args.baseline_benchmark_raw)) if args.baseline_benchmark_raw else [],
        baseline_judge_summary=_load_json(Path(args.baseline_judge_summary)) if args.baseline_judge_summary else {},
        baseline_judge_rows=load_jsonl(Path(args.baseline_judge_raw)) if args.baseline_judge_raw else [],
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_json = out_dir / "review_summary.json"
    summary_md = out_dir / "review_summary.md"
    regressions_jsonl = out_dir / "regression_cases.jsonl"
    review_queue_jsonl = out_dir / "human_review_queue.jsonl"
    summary_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(summary_md, report)
    _write_jsonl(regressions_jsonl, list(report.get("regression_cases", [])))
    _write_jsonl(review_queue_jsonl, list(report.get("human_review_queue", [])))
    print(
        f"[review] regressions={len(report.get('regression_cases', []))} "
        f"queue={len(report.get('human_review_queue', []))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
