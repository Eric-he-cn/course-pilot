from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.metrics import estimate_prompt_tokens, trace_scope  # noqa: E402
from core.orchestration.runner import OrchestrationRunner  # noqa: E402
from core.llm.openai_compat import get_llm_client  # noqa: E402
from mcp_tools.client import MCPTools  # noqa: E402


def _trim(s: Any, n: int = 220) -> str:
    t = str(s or "").strip().replace("\n", " ")
    if len(t) <= n:
        return t
    return t[:n].rstrip() + "..."


def _usage_tokens(response: Any):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None
    p = getattr(usage, "prompt_tokens", None)
    c = getattr(usage, "completion_tokens", None)
    return p, c


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            out.append(json.loads(s))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace round IO with tool args/results")
    parser.add_argument("--case-id", default="learn_01")
    parser.add_argument("--cases", default=str(ROOT / "benchmarks" / "cases_v1.jsonl"))
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "perf_runs" / "trace_debug"))
    args = parser.parse_args()

    cases = _load_jsonl(Path(args.cases))
    case = None
    for c in cases:
        if str(c.get("case_id", "")).strip() == args.case_id:
            case = c
            break
    if case is None:
        raise SystemExit(f"case not found: {args.case_id}")

    course_name = str(case.get("course_name", "矩阵理论"))
    mode = str(case.get("mode", "learn"))
    message = str(case.get("message", ""))
    history = case.get("history") or []

    llm_calls: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []

    llm = get_llm_client()
    orig_create = llm.client.chat.completions.create
    call_idx = {"n": 0}

    def wrapped_create(*a, **kw):
        call_idx["n"] += 1
        idx = call_idx["n"]
        messages = kw.get("messages") or []
        stream = bool(kw.get("stream", False))
        tools = kw.get("tools")
        with_tools = bool(tools)
        est = estimate_prompt_tokens(messages)
        last_user = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = str(m.get("content", ""))
                break
        req = {
            "idx": idx,
            "stream": stream,
            "with_tools": with_tools,
            "messages_len": len(messages),
            "prompt_tokens_est": est,
            "last_user_excerpt": _trim(last_user, 260),
        }
        resp = orig_create(*a, **kw)
        if not stream:
            msg = resp.choices[0].message
            content = getattr(msg, "content", "") or ""
            tc = getattr(msg, "tool_calls", None) or []
            tool_names = [x.function.name for x in tc]
            p, c = _usage_tokens(resp)
            req.update(
                {
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "tool_calls_requested": tool_names,
                    "output_excerpt": _trim(content, 260),
                }
            )
            llm_calls.append(req)
            return resp

        # stream=True: wrap generator to capture output summary
        def _iter():
            out_parts: List[str] = []
            prompt_tokens = None
            completion_tokens = None
            first_delta_ms = None
            t0 = perf_counter()
            for chunk in resp:
                try:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        if getattr(usage, "prompt_tokens", None) is not None:
                            prompt_tokens = getattr(usage, "prompt_tokens", None)
                        if getattr(usage, "completion_tokens", None) is not None:
                            completion_tokens = getattr(usage, "completion_tokens", None)
                    choices = getattr(chunk, "choices", None)
                    if isinstance(choices, list) and choices:
                        delta = getattr(choices[0], "delta", None)
                        txt = getattr(delta, "content", None) if delta is not None else None
                        if txt:
                            if first_delta_ms is None:
                                first_delta_ms = (perf_counter() - t0) * 1000.0
                            out_parts.append(txt)
                except Exception:
                    pass
                yield chunk
            req.update(
                {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "first_delta_ms": first_delta_ms,
                    "tool_calls_requested": [],
                    "output_excerpt": _trim("".join(out_parts), 260),
                }
            )
            llm_calls.append(req)

        return _iter()

    orig_tool = MCPTools.call_tool
    tool_idx = {"n": 0}

    def wrapped_tool(tool_name: str, **kwargs):
        tool_idx["n"] += 1
        i = tool_idx["n"]
        t0 = perf_counter()
        res = orig_tool(tool_name, **kwargs)
        elapsed = (perf_counter() - t0) * 1000.0
        tool_calls.append(
            {
                "idx": i,
                "tool_name": tool_name,
                "args": kwargs,
                "elapsed_ms": elapsed,
                "success": bool(res.get("success", False)) if isinstance(res, dict) else False,
                "result_excerpt": _trim(res, 260),
            }
        )
        return res

    llm.client.chat.completions.create = wrapped_create
    MCPTools.call_tool = wrapped_tool

    runner = OrchestrationRunner()
    t0 = perf_counter()
    text = []
    statuses = []

    try:
        with trace_scope({"profile": "round_io_trace", "case_id": args.case_id, "mode": mode}):
            for chunk in runner.run_stream(
                course_name=course_name,
                mode=mode,
                user_message=message,
                state={},
                history=history,
            ):
                if isinstance(chunk, str):
                    text.append(chunk)
                elif isinstance(chunk, dict) and isinstance(chunk.get("__status__"), str):
                    statuses.append(chunk.get("__status__"))
    finally:
        llm.client.chat.completions.create = orig_create
        MCPTools.call_tool = orig_tool

    e2e = (perf_counter() - t0) * 1000.0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = out_dir / f"{stamp}_{args.case_id}_round_io.json"

    data = {
        "meta": {
            "case_id": args.case_id,
            "mode": mode,
            "course_name": course_name,
            "message": message,
            "e2e_ms": e2e,
        },
        "statuses": statuses,
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
        "response_preview": _trim("".join(text), 600),
    }
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[round-io] e2e_ms={e2e:.1f} llm_calls={len(llm_calls)} tool_calls={len(tool_calls)}")
    print(f"[round-io] json={out_json}")

    for c in llm_calls:
        print("-" * 120)
        print(
            f"LLM#{c['idx']} stream={int(bool(c.get('stream')))} with_tools={int(bool(c.get('with_tools')))} "
            f"prompt={c.get('prompt_tokens')} est={c.get('prompt_tokens_est')} completion={c.get('completion_tokens')}"
        )
        print(f"  input(last_user): {c.get('last_user_excerpt','')}")
        req_tools = c.get("tool_calls_requested") or []
        print(f"  output(tool_calls): {req_tools}")
        print(f"  output(text): {c.get('output_excerpt','')}")

    if tool_calls:
        print("=" * 120)
        print("TOOL CALLS")
        for t in tool_calls:
            print(
                f"tool#{t['idx']} name={t['tool_name']} success={int(bool(t['success']))} elapsed_ms={t['elapsed_ms']:.1f}"
            )
            print(f"  args: {_trim(t.get('args'), 320)}")
            print(f"  result: {t.get('result_excerpt','')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
