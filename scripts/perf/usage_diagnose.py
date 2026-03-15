"""Diagnose token usage availability by LLM call type from trace events."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List

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


def _event_type_name(event: Dict[str, Any]) -> str:
    if bool(event.get("stream")):
        return "stream"
    if bool(event.get("with_tools")) or bool(event.get("stream_tools")):
        return "tools"
    return "nonstream"


def _new_counter() -> Dict[str, int]:
    return {
        "events": 0,
        "ok_events": 0,
        "prompt_present": 0,
        "completion_present": 0,
        "both_present": 0,
        "missing_any": 0,
        "missing_both": 0,
    }


def _bump(counter: Dict[str, int], event: Dict[str, Any]) -> None:
    counter["events"] += 1
    if bool(event.get("success")):
        counter["ok_events"] += 1
    has_prompt = isinstance(event.get("prompt_tokens"), (int, float))
    has_completion = isinstance(event.get("completion_tokens"), (int, float))
    if has_prompt:
        counter["prompt_present"] += 1
    if has_completion:
        counter["completion_present"] += 1
    if has_prompt and has_completion:
        counter["both_present"] += 1
    if not (has_prompt and has_completion):
        counter["missing_any"] += 1
    if (not has_prompt) and (not has_completion):
        counter["missing_both"] += 1


def _to_rate(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return float(value) / float(total)


def _summarize_counter(counter: Dict[str, int]) -> Dict[str, Any]:
    n = int(counter["events"])
    return {
        **counter,
        "ok_rate": _to_rate(counter["ok_events"], n),
        "prompt_present_rate": _to_rate(counter["prompt_present"], n),
        "completion_present_rate": _to_rate(counter["completion_present"], n),
        "both_present_rate": _to_rate(counter["both_present"], n),
        "missing_any_rate": _to_rate(counter["missing_any"], n),
        "missing_both_rate": _to_rate(counter["missing_both"], n),
    }


def _write_markdown(path: Path, report: Dict[str, Any]) -> None:
    def fmt(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:.4f}"
        return str(v)

    lines: List[str] = [
        f"# Usage 缺失诊断 - {report.get('profile', 'usage_diagnose')}",
        "",
        f"- cases: {report.get('cases_total', 0)}",
        f"- llm_events: {report.get('llm_events_total', 0)}",
        f"- generated_at: {report.get('generated_at', '')}",
        "",
        "## 按调用类型",
        "",
        "| type | events | ok_rate | prompt_present_rate | completion_present_rate | both_present_rate | missing_both_rate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("nonstream", "stream", "tools"):
        block = report.get("by_type", {}).get(key, {})
        lines.append(
            f"| `{key}` | {fmt(block.get('events', 0))} | {fmt(block.get('ok_rate', 0.0))} | "
            f"{fmt(block.get('prompt_present_rate', 0.0))} | {fmt(block.get('completion_present_rate', 0.0))} | "
            f"{fmt(block.get('both_present_rate', 0.0))} | {fmt(block.get('missing_both_rate', 0.0))} |"
        )

    lines.extend(
        [
            "",
            "## 按 provider",
            "",
            "| provider | events | both_present_rate | missing_both_rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for provider, block in sorted(report.get("by_provider", {}).items()):
        lines.append(
            f"| `{provider}` | {fmt(block.get('events', 0))} | "
            f"{fmt(block.get('both_present_rate', 0.0))} | {fmt(block.get('missing_both_rate', 0.0))} |"
        )

    missing_examples = report.get("missing_examples", [])
    if missing_examples:
        lines.extend(["", "## 缺失样本（前 20 条）", ""])
        lines.append("| case_id | mode | seq | type | stream | with_tools | success | error |")
        lines.append("|---|---|---:|---|---|---|---|---|")
        for row in missing_examples[:20]:
            lines.append(
                f"| `{row.get('case_id','')}` | `{row.get('mode','')}` | {row.get('seq',0)} | "
                f"`{row.get('type','')}` | {row.get('stream', False)} | {row.get('with_tools', False)} | "
                f"{row.get('success', False)} | `{row.get('error','')}` |"
            )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose usage availability from llm_call trace events.")
    parser.add_argument("--cases", default=str(ROOT / "benchmarks" / "cases_v1.jsonl"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "usage_diagnose"))
    parser.add_argument("--profile", default="usage_diagnose")
    parser.add_argument("--max-cases", type=int, default=9)
    parser.add_argument("--per-mode", type=int, default=3)
    args = parser.parse_args()

    cases = _load_jsonl(Path(args.cases))
    if not cases:
        print(f"[usage_diag] no cases found: {args.cases}")
        return 1

    # Keep balanced coverage across learn/practice/exam by default.
    selected: List[Dict[str, Any]] = []
    mode_counts: Dict[str, int] = defaultdict(int)
    for c in cases:
        mode = str(c.get("mode", "learn"))
        if mode_counts[mode] >= int(args.per_mode):
            continue
        selected.append(c)
        mode_counts[mode] += 1
        if len(selected) >= int(args.max_cases):
            break
    if not selected:
        selected = cases[: max(1, int(args.max_cases))]

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "usage_diagnose.json"
    md_path = out_dir / "usage_diagnose.md"

    runner = OrchestrationRunner()
    by_type_raw: Dict[str, Dict[str, int]] = {
        "nonstream": _new_counter(),
        "stream": _new_counter(),
        "tools": _new_counter(),
    }
    by_provider_raw: Dict[str, Dict[str, int]] = defaultdict(_new_counter)
    by_mode_raw: Dict[str, Dict[str, int]] = defaultdict(_new_counter)
    missing_examples: List[Dict[str, Any]] = []

    total_llm_events = 0
    total_cases = len(selected)
    print(f"[usage_diag] profile={args.profile} cases={total_cases}")

    for idx, case in enumerate(selected, start=1):
        case_id = str(case.get("case_id", "")).strip()
        mode = str(case.get("mode", "learn")).strip()
        course_name = str(case.get("course_name", "")).strip()
        message = str(case.get("message", ""))
        history = case.get("history") or []
        if not isinstance(history, list):
            history = []

        t0 = perf_counter()
        with trace_scope({"case_id": case_id, "mode": mode, "diagnose": True}) as trace:
            try:
                for _ in runner.run_stream(
                    course_name=course_name,
                    mode=mode,
                    user_message=message,
                    state={},
                    history=history,
                ):
                    pass
            except Exception:
                pass
            events = list(trace.events)

        llm_events = [e for e in events if e.get("type") == "llm_call"]
        total_llm_events += len(llm_events)
        elapsed = (perf_counter() - t0) * 1000.0
        print(f"[usage_diag] {idx}/{total_cases} {case_id} llm_events={len(llm_events)} e2e={elapsed:.1f}ms")

        for e in llm_events:
            tname = _event_type_name(e)
            _bump(by_type_raw[tname], e)
            provider = str(e.get("provider", "unknown") or "unknown")
            _bump(by_provider_raw[provider], e)
            _bump(by_mode_raw[mode], e)

            has_prompt = isinstance(e.get("prompt_tokens"), (int, float))
            has_completion = isinstance(e.get("completion_tokens"), (int, float))
            if not (has_prompt and has_completion):
                missing_examples.append(
                    {
                        "case_id": case_id,
                        "mode": mode,
                        "seq": e.get("seq", 0),
                        "type": tname,
                        "stream": bool(e.get("stream")),
                        "with_tools": bool(e.get("with_tools")) or bool(e.get("stream_tools")),
                        "success": bool(e.get("success")),
                        "error": str(e.get("error", "") or ""),
                        "prompt_tokens": e.get("prompt_tokens"),
                        "completion_tokens": e.get("completion_tokens"),
                        "prompt_tokens_est": e.get("prompt_tokens_est"),
                    }
                )

    report: Dict[str, Any] = {
        "profile": args.profile,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases_total": total_cases,
        "llm_events_total": total_llm_events,
        "by_type": {k: _summarize_counter(v) for k, v in by_type_raw.items()},
        "by_provider": {k: _summarize_counter(v) for k, v in by_provider_raw.items()},
        "by_mode": {k: _summarize_counter(v) for k, v in by_mode_raw.items()},
        "missing_examples": missing_examples[:200],
    }

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(md_path, report)
    print(f"[usage_diag] report_json={json_path}")
    print(f"[usage_diag] report_md={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

