"""Trace one benchmark case and print per-call LLM/tool/retrieval details."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.metrics import estimate_prompt_tokens, trace_scope  # noqa: E402
from core.llm.openai_compat import get_llm_client  # noqa: E402
from core.orchestration.runner import OrchestrationRunner  # noqa: E402
from mcp_tools.client import MCPTools  # noqa: E402


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


def _pick_case(args: argparse.Namespace) -> Dict[str, Any]:
    if args.message:
        return {
            "case_id": args.case_id or "adhoc_case",
            "mode": args.mode,
            "course_name": args.course_name,
            "message": args.message,
            "history": [],
        }
    cases = _load_jsonl(Path(args.cases))
    if not cases:
        raise RuntimeError(f"no cases found: {args.cases}")
    if args.case_id:
        for row in cases:
            if str(row.get("case_id", "")).strip() == args.case_id:
                return row
        raise RuntimeError(f"case_id not found: {args.case_id}")
    return cases[0]


def _fmt_ms(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{float(v):.1f}"
    return "-"


def _fmt_num(v: Any) -> str:
    if isinstance(v, (int, float)):
        return str(int(v))
    return "-"


def _trim_text(v: Any, max_chars: int = 260) -> str:
    s = str(v or "").strip().replace("\r", " ").replace("\n", " ")
    if len(s) <= max_chars:
        return s
    return s[:max_chars].rstrip() + "..."


def _usage_tokens(response: Any) -> tuple[Optional[int], Optional[int]]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    p = getattr(usage, "prompt_tokens", None)
    c = getattr(usage, "completion_tokens", None)
    p = int(p) if isinstance(p, (int, float)) else None
    c = int(c) if isinstance(c, (int, float)) else None
    return p, c


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace one case with per-call details.")
    parser.add_argument("--cases", default=str(ROOT / "benchmarks" / "cases_v1.jsonl"))
    parser.add_argument("--case-id", default="")
    parser.add_argument("--course-name", default="矩阵理论")
    parser.add_argument("--mode", default="learn", choices=["learn", "practice", "exam"])
    parser.add_argument("--message", default="")
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "trace_debug"))
    parser.add_argument("--profile", default="trace_case")
    args = parser.parse_args()

    case = _pick_case(args)
    case_id = str(case.get("case_id", "adhoc_case")).strip()
    mode = str(case.get("mode", args.mode)).strip()
    course_name = str(case.get("course_name", args.course_name)).strip()
    message = str(case.get("message", args.message))
    history = case.get("history") or []
    if not isinstance(history, list):
        history = []

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"{stamp}_{case_id}_trace.json"
    out_md = out_dir / f"{stamp}_{case_id}_trace.md"

    llm_round_io: List[Dict[str, Any]] = []
    tool_round_io: List[Dict[str, Any]] = []

    llm = get_llm_client()
    original_create = llm.client.chat.completions.create
    create_idx = {"n": 0}

    def _wrapped_create(*a, **kw):
        create_idx["n"] += 1
        idx = int(create_idx["n"])
        messages = kw.get("messages") or []
        stream = bool(kw.get("stream", False))
        with_tools = bool(kw.get("tools"))
        prompt_est = estimate_prompt_tokens(messages)
        last_user = ""
        if isinstance(messages, list):
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    last_user = str(msg.get("content", ""))
                    break
        snap = {
            "idx": idx,
            "stream": stream,
            "with_tools": with_tools,
            "messages_len": len(messages) if isinstance(messages, list) else 0,
            "prompt_tokens_est": prompt_est,
            "input_last_user_excerpt": _trim_text(last_user, 360),
            "requested_tools": [],
            "output_excerpt": "",
            "prompt_tokens": None,
            "completion_tokens": None,
            "first_delta_ms": None,
        }

        response = original_create(*a, **kw)
        if not stream:
            try:
                msg = response.choices[0].message
                content = getattr(msg, "content", "") or ""
                tool_calls = getattr(msg, "tool_calls", None) or []
                snap["requested_tools"] = [tc.function.name for tc in tool_calls]
                snap["output_excerpt"] = _trim_text(content, 360)
            except Exception:
                pass
            p, c = _usage_tokens(response)
            snap["prompt_tokens"] = p
            snap["completion_tokens"] = c
            llm_round_io.append(snap)
            return response

        def _iter():
            out_parts: List[str] = []
            p_tokens: Optional[int] = None
            c_tokens: Optional[int] = None
            first_delta_ms: Optional[float] = None
            t_stream = perf_counter()
            for chunk in response:
                p, c = _usage_tokens(chunk)
                if p is not None:
                    p_tokens = p
                if c is not None:
                    c_tokens = c
                choices = getattr(chunk, "choices", None)
                if isinstance(choices, list) and choices:
                    delta = getattr(choices[0], "delta", None)
                    txt = getattr(delta, "content", None) if delta is not None else None
                    if txt:
                        if first_delta_ms is None:
                            first_delta_ms = (perf_counter() - t_stream) * 1000.0
                        out_parts.append(txt)
                yield chunk
            snap["prompt_tokens"] = p_tokens
            snap["completion_tokens"] = c_tokens
            snap["first_delta_ms"] = first_delta_ms
            snap["output_excerpt"] = _trim_text("".join(out_parts), 360)
            llm_round_io.append(snap)

        return _iter()

    original_call_tool = MCPTools.call_tool
    tool_idx = {"n": 0}

    def _wrapped_call_tool(tool_name: str, **kwargs):
        tool_idx["n"] += 1
        idx = int(tool_idx["n"])
        t0 = perf_counter()
        result = original_call_tool(tool_name, **kwargs)
        elapsed_ms = (perf_counter() - t0) * 1000.0
        tool_round_io.append(
            {
                "idx": idx,
                "tool_name": tool_name,
                "args": kwargs,
                "elapsed_ms": elapsed_ms,
                "success": bool(result.get("success", False)) if isinstance(result, dict) else False,
                "result_excerpt": _trim_text(result, 360),
            }
        )
        return result

    llm.client.chat.completions.create = _wrapped_create
    MCPTools.call_tool = _wrapped_call_tool

    runner = OrchestrationRunner()
    text_parts: List[str] = []
    statuses: List[str] = []
    citations: List[Dict[str, Any]] = []

    t0 = perf_counter()
    first_chunk_ms: Optional[float] = None
    case_error = None
    events: List[Dict[str, Any]] = []
    try:
        with trace_scope(
            {
                "profile": args.profile,
                "case_id": case_id,
                "mode": mode,
                "course_name": course_name,
            }
        ) as trace:
            for chunk in runner.run_stream(
                course_name=course_name,
                mode=mode,
                user_message=message,
                state={},
                history=history,
            ):
                if isinstance(chunk, str):
                    if chunk and first_chunk_ms is None:
                        first_chunk_ms = (perf_counter() - t0) * 1000.0
                    text_parts.append(chunk)
                elif isinstance(chunk, dict):
                    st = chunk.get("__status__")
                    if isinstance(st, str) and st.strip():
                        statuses.append(st.strip())
                    cits = chunk.get("__citations__")
                    if isinstance(cits, list):
                        citations = cits
            events = list(trace.events)
    except Exception as ex:
        case_error = f"{type(ex).__name__}: {ex}"
    finally:
        llm.client.chat.completions.create = original_create
        MCPTools.call_tool = original_call_tool

    e2e_ms = (perf_counter() - t0) * 1000.0
    response_text = "".join(text_parts)

    llm_events = [e for e in events if e.get("type") == "llm_call"]
    retrieval_events = [e for e in events if e.get("type") == "retrieval"]
    tool_events = [e for e in events if e.get("type") == "tool_call"]
    tool_dedup_events = [e for e in events if e.get("type") == "tool_dedup"]
    budget_events = [e for e in events if e.get("type") == "context_budget"]

    llm_rows: List[Dict[str, Any]] = []
    for idx, e in enumerate(llm_events, start=1):
        prompt_tokens = e.get("prompt_tokens")
        prompt_est = e.get("prompt_tokens_est")
        if isinstance(prompt_tokens, (int, float)):
            prompt_source = "usage"
        elif isinstance(prompt_est, (int, float)) and float(prompt_est) > 0:
            prompt_source = "estimate"
        else:
            prompt_source = "none"
        row = {
            "idx": idx,
            "round": e.get("round"),
            "stream": bool(e.get("stream")),
            "with_tools": bool(e.get("with_tools")),
            "tool_round_count": e.get("tool_round_count"),
            "final_output_source": e.get("final_output_source", ""),
            "final_output_regen": bool(e.get("final_output_regen")),
            "llm_ms": e.get("llm_ms"),
            "first_token_latency_ms": e.get("first_token_latency_ms"),
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_est": prompt_est,
            "prompt_tokens_source": prompt_source,
            "completion_tokens": e.get("completion_tokens"),
        }
        llm_rows.append(row)

    result = {
        "meta": {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "profile": args.profile,
            "case_id": case_id,
            "mode": mode,
            "course_name": course_name,
            "message": message,
            "history_len": len(history),
        },
        "summary": {
            "case_error": case_error,
            "e2e_ms": e2e_ms,
            "first_chunk_ms": first_chunk_ms,
            "response_chars": len(response_text),
            "citations_count": len(citations),
            "llm_call_count": len(llm_rows),
            "retrieval_call_count": len(retrieval_events),
            "tool_call_count": len(tool_events),
            "tool_dedup_event_count": len(tool_dedup_events),
        },
        "statuses": statuses,
        "llm_calls": llm_rows,
        "llm_round_io": llm_round_io,
        "retrieval_events": retrieval_events,
        "tool_events": tool_events,
        "tool_round_io": tool_round_io,
        "tool_dedup_events": tool_dedup_events,
        "context_budget_events": budget_events,
        "response_preview": response_text[:1200],
    }
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: List[str] = []
    lines.append(f"# Trace Case - {case_id}")
    lines.append("")
    lines.append(f"- mode: `{mode}`")
    lines.append(f"- course: `{course_name}`")
    lines.append(f"- e2e_ms: `{e2e_ms:.1f}`")
    lines.append(f"- first_chunk_ms: `{first_chunk_ms if first_chunk_ms is not None else '-'} `")
    lines.append(f"- llm_calls: `{len(llm_rows)}`")
    lines.append(f"- retrieval_calls: `{len(retrieval_events)}`")
    lines.append(f"- tool_calls: `{len(tool_events)}`")
    lines.append(f"- dedup_events: `{len(tool_dedup_events)}`")
    lines.append("")
    lines.append("## LLM Calls")
    lines.append("")
    lines.append("| idx | round | stream | with_tools | tool_round_count | final_output_source | llm_ms | first_token_ms | prompt_tokens | prompt_est | source | completion_tokens |")
    lines.append("|---:|---|:---:|:---:|---:|---|---:|---:|---:|---:|---|---:|")
    for r in llm_rows:
        lines.append(
            f"| {r['idx']} | {r.get('round', '-')} | {int(bool(r.get('stream')))} | {int(bool(r.get('with_tools')))} | "
            f"{_fmt_num(r.get('tool_round_count'))} | {r.get('final_output_source', '')} | {_fmt_ms(r.get('llm_ms'))} | "
            f"{_fmt_ms(r.get('first_token_latency_ms'))} | {_fmt_num(r.get('prompt_tokens'))} | "
            f"{_fmt_num(r.get('prompt_tokens_est'))} | {r.get('prompt_tokens_source', '')} | {_fmt_num(r.get('completion_tokens'))} |"
        )
    lines.append("")
    lines.append("## Status Events")
    lines.append("")
    for s in statuses:
        lines.append(f"- {s}")
    lines.append("")
    lines.append("## Context Budget Events")
    lines.append("")
    for e in budget_events:
        lines.append(
            "- "
            f"history={e.get('history_tokens_est', '-')}, "
            f"history_recent={e.get('history_recent_tokens_est', '-')}, "
            f"history_summary={e.get('history_summary_tokens_est', '-')}, "
            f"source={e.get('history_summary_source', '-')}, "
            f"llm_compress={e.get('history_llm_compress_applied', '-')}, "
            f"rag={e.get('rag_tokens_est', '-')}, "
            f"memory={e.get('memory_tokens_est', '-')}, "
            f"final={e.get('final_tokens_est', '-')}"
        )
    lines.append("")
    lines.append("## LLM Round IO")
    lines.append("")
    lines.append("| idx | stream | with_tools | prompt_tokens | prompt_est | completion_tokens | requested_tools | input_last_user_excerpt | output_excerpt |")
    lines.append("|---:|:---:|:---:|---:|---:|---:|---|---|---|")
    for r in llm_round_io:
        tools_str = ",".join([str(x) for x in (r.get("requested_tools") or [])])
        lines.append(
            f"| {r.get('idx', '-')} | {int(bool(r.get('stream')))} | {int(bool(r.get('with_tools')))} | "
            f"{_fmt_num(r.get('prompt_tokens'))} | {_fmt_num(r.get('prompt_tokens_est'))} | {_fmt_num(r.get('completion_tokens'))} | "
            f"{tools_str} | {str(r.get('input_last_user_excerpt', '')).replace('|', '/')} | {str(r.get('output_excerpt', '')).replace('|', '/')} |"
        )
    lines.append("")
    lines.append("## Tool Round IO")
    lines.append("")
    lines.append("| idx | tool_name | success | elapsed_ms | args | result_excerpt |")
    lines.append("|---:|---|:---:|---:|---|---|")
    for t in tool_round_io:
        lines.append(
            f"| {t.get('idx', '-')} | {t.get('tool_name', '')} | {int(bool(t.get('success')))} | "
            f"{_fmt_ms(t.get('elapsed_ms'))} | {str(t.get('args', '')).replace('|', '/')} | "
            f"{str(t.get('result_excerpt', '')).replace('|', '/')} |"
        )

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"[trace] case={case_id} mode={mode} e2e_ms={e2e_ms:.1f} llm_calls={len(llm_rows)}")
    print(f"[trace] json={out_json}")
    print(f"[trace] md={out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



