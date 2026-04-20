"""Lint benchmark datasets and report schema/coverage quality."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


REQUIRED_KEYS = ("case_id", "mode", "course_name", "message", "history")
VALID_MODES = {"learn", "practice", "exam"}


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


def _iter_case_files(path: Path) -> Iterable[Path]:
    legacy_skip = {
        "cases_v1.jsonl",
        "cases_v1_top5.jsonl",
        "cases_v1_smoke3.jsonl",
        "cases_smoke.jsonl",
        "gold_candidates.jsonl",
        "gold_manual_fix.jsonl",
        "gold_rejected.jsonl",
        "gold_label_sessions.jsonl",
    }
    if path.is_file():
        yield path
        return
    yielded = False
    for child in sorted(path.glob("*.jsonl")):
        if "rag_gold" in child.name or child.name in legacy_skip:
            continue
        if child.stat().st_size <= 0:
            continue
        yielded = True
        yield child
    if yielded:
        return
    fallback = path / "archive" / "20260415_legacy_reset" / "v3_expanded_84.jsonl"
    if path.name == "benchmarks" and fallback.exists() and fallback.stat().st_size > 0:
        yield fallback


def _is_session_case(row: Dict[str, Any]) -> bool:
    expected = row.get("expected_session_events")
    if isinstance(expected, list) and expected:
        return True
    tags = [str(tag).strip().lower() for tag in row.get("tags", []) if str(tag).strip()]
    return any(tag in {"session", "session_resume", "multi_turn"} for tag in tags)


def _is_tool_case(row: Dict[str, Any]) -> bool:
    if bool(row.get("should_use_tools")):
        return True
    tags = [str(tag).strip().lower() for tag in row.get("tags", []) if str(tag).strip()]
    return any(tag in {"tool", "tools", "permission", "tooling"} for tag in tags)


def _is_fallback_or_route_case(row: Dict[str, Any]) -> bool:
    tags = [str(tag).strip().lower() for tag in row.get("tags", []) if str(tag).strip()]
    return any(tag in {"fallback", "route_override", "error_recovery"} for tag in tags)


def lint_cases(
    rows: List[Dict[str, Any]],
    *,
    min_courses: int = 0,
    min_multi_turn_ratio: float = 0.0,
    min_session_ratio: float = 0.0,
    min_tool_ratio: float = 0.0,
    min_fallback_ratio: float = 0.0,
) -> Dict[str, Any]:
    errors: List[str] = []
    case_ids: List[str] = []
    mode_counts: Counter[str] = Counter()
    course_counts: Counter[str] = Counter()
    history_nonempty = 0
    session_cases = 0
    tool_cases = 0
    fallback_cases = 0

    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"row_{idx}: not_object")
            continue
        for key in REQUIRED_KEYS:
            if key not in row:
                errors.append(f"row_{idx}: missing_{key}")
        case_id = str(row.get("case_id", "")).strip()
        if case_id:
            case_ids.append(case_id)
        else:
            errors.append(f"row_{idx}: empty_case_id")
        mode = str(row.get("mode", "")).strip()
        if mode not in VALID_MODES:
            errors.append(f"row_{idx}: invalid_mode={mode}")
        else:
            mode_counts[mode] += 1
        course_name = str(row.get("course_name", "")).strip()
        if not course_name:
            errors.append(f"row_{idx}: empty_course_name")
        else:
            course_counts[course_name] += 1
        history = row.get("history")
        if not isinstance(history, list):
            errors.append(f"row_{idx}: history_not_list")
        elif history:
            history_nonempty += 1

        if row.get("requires_citations") and not isinstance(row.get("gold_doc_ids", []), list):
            errors.append(f"row_{idx}: gold_doc_ids_required")

        if _is_session_case(row):
            session_cases += 1
        if _is_tool_case(row):
            tool_cases += 1
        if _is_fallback_or_route_case(row):
            fallback_cases += 1

    total = len(rows)
    duplicate_case_ids = sorted([cid for cid, count in Counter(case_ids).items() if count > 1])
    if duplicate_case_ids:
        errors.append(f"duplicate_case_ids={','.join(duplicate_case_ids)}")

    history_ratio = float(history_nonempty) / float(total) if total else 0.0
    session_ratio = float(session_cases) / float(total) if total else 0.0
    tool_ratio = float(tool_cases) / float(total) if total else 0.0
    fallback_ratio = float(fallback_cases) / float(total) if total else 0.0

    coverage_failures: List[str] = []
    if min_courses and len(course_counts) < min_courses:
        coverage_failures.append(f"course_count<{min_courses}")
    if min_multi_turn_ratio and history_ratio < min_multi_turn_ratio:
        coverage_failures.append(f"history_ratio<{min_multi_turn_ratio:.2f}")
    if min_session_ratio and session_ratio < min_session_ratio:
        coverage_failures.append(f"session_ratio<{min_session_ratio:.2f}")
    if min_tool_ratio and tool_ratio < min_tool_ratio:
        coverage_failures.append(f"tool_ratio<{min_tool_ratio:.2f}")
    if min_fallback_ratio and fallback_ratio < min_fallback_ratio:
        coverage_failures.append(f"fallback_ratio<{min_fallback_ratio:.2f}")

    return {
        "total_cases": total,
        "mode_counts": dict(mode_counts),
        "course_counts": dict(course_counts),
        "duplicate_case_ids": duplicate_case_ids,
        "history_nonempty_ratio": history_ratio,
        "session_case_ratio": session_ratio,
        "tool_case_ratio": tool_ratio,
        "fallback_or_route_case_ratio": fallback_ratio,
        "errors": errors,
        "coverage_failures": coverage_failures,
        "ok": not errors and not coverage_failures,
    }


def _write_markdown(path: Path, report: Dict[str, Any]) -> None:
    lines = [
        "# Dataset Lint Report",
        "",
        f"- total_cases: {report.get('total_cases', 0)}",
        f"- ok: {report.get('ok', False)}",
        "",
        "## Coverage",
        "",
        f"- history_nonempty_ratio: {report.get('history_nonempty_ratio', 0.0):.4f}",
        f"- session_case_ratio: {report.get('session_case_ratio', 0.0):.4f}",
        f"- tool_case_ratio: {report.get('tool_case_ratio', 0.0):.4f}",
        f"- fallback_or_route_case_ratio: {report.get('fallback_or_route_case_ratio', 0.0):.4f}",
        "",
        "## Modes",
        "",
    ]
    for mode, count in sorted((report.get("mode_counts") or {}).items()):
        lines.append(f"- {mode}: {count}")
    lines.extend(["", "## Courses", ""])
    for course_name, count in sorted((report.get("course_counts") or {}).items()):
        lines.append(f"- {course_name}: {count}")
    if report.get("coverage_failures"):
        lines.extend(["", "## Coverage Failures", ""])
        for item in report["coverage_failures"]:
            lines.append(f"- {item}")
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for item in report["errors"]:
            lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint benchmark datasets for schema and coverage.")
    parser.add_argument("--path", default=str(ROOT / "benchmarks"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "dataset_lint"))
    parser.add_argument("--min-courses", type=int, default=4)
    parser.add_argument("--min-multi-turn-ratio", type=float, default=0.25)
    parser.add_argument("--min-session-ratio", type=float, default=0.15)
    parser.add_argument("--min-tool-ratio", type=float, default=0.15)
    parser.add_argument("--min-fallback-ratio", type=float, default=0.10)
    args = parser.parse_args()

    base = Path(args.path)
    rows: List[Dict[str, Any]] = []
    for case_file in _iter_case_files(base):
        rows.extend(load_jsonl(case_file))

    report = lint_cases(
        rows,
        min_courses=max(0, int(args.min_courses)),
        min_multi_turn_ratio=max(0.0, float(args.min_multi_turn_ratio)),
        min_session_ratio=max(0.0, float(args.min_session_ratio)),
        min_tool_ratio=max(0.0, float(args.min_tool_ratio)),
        min_fallback_ratio=max(0.0, float(args.min_fallback_ratio)),
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dataset_lint.json"
    md_path = out_dir / "dataset_lint.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(md_path, report)
    print(f"[dataset_lint] total={report['total_cases']} ok={int(bool(report['ok']))}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
