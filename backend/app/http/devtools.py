"""开发者模式的 trace 视图：把一轮的执行流程和它取回的正文合成一份。

两个来源缺一不可：trace 给 ReAct 时序（每轮的思考、说的话、发起的调用）、耗时、参数与
usage，messages 表 role='tool' 的行给工具取回的正文（trace 里只有「命中 6 段」这样的摘要）。
正文按需取：列表里只报它有多长，点开某一步才走 tool_body() 单独拉一条。

这条链路只服务观测。trace 写失败只打 warning、payload 可以被单独清理、整个目录也能删，
所以业务功能不该拿它当数据源——这里读不到就如实报空，不要在这里补数据。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Sequence

from modules.agent.tools import PERSISTED_TOOL_BODIES
from modules.agent.trace import (MAX_DAY_FILES, MAX_PAYLOAD_BYTES, MAX_PAYLOAD_TOTAL_BYTES,
                                 MAX_SCAN_LINES, MAX_TURNS, REACT_FIELD_MAX_CHARS,
                                 REACT_TURN_MAX_CHARS, TRACE_SUBDIR, TraceReader,
                                 is_seed_call, is_subagent_call)
from modules.sessions.api import Message, SessionSummary

__all__ = ["MAX_DAY_FILES", "MAX_PAYLOAD_BYTES", "MAX_PAYLOAD_TOTAL_BYTES", "MAX_SCAN_LINES",
           "MAX_TURNS", "TRACE_SUBDIR", "build_trace_view", "tool_body"]

# 已经有专门字段承载的键。其余的原样进 extras——trace 的字段随功能增加，
# 读端按白名单挑会静默丢掉新加的那些。
_CARRIED = frozenset({
    "kind", "session_id", "turn_id", "started_at", "status", "error_code", "duration_ms",
    "scope_mode", "prompt_version", "answer_chars", "citations", "citations_retrieved",
    "tool_rounds", "resolution", "responder", "usage", "tools", "payload_ref", "react",
})
_SPAN_FIELDS = ("round", "origin", "name", "ok", "summary", "summary_key", "summary_args",
                "duration_ms", "decision", "reason", "reused")
# finish_reason 是厂商原样返回的，outcome 是服务端自己的判断。两者含义不同，都要带出来。
_STEP_FIELDS = ("round", "injected", "outcome", "finish_reason", "reasoning_field", "calls",
                "reasoning", "reasoning_chars", "text", "text_chars")


def build_trace_view(*, session: SessionSummary, messages: Sequence[Message],
                     data_dir: Path, focus_turn_id: str | None) -> dict:
    reader = TraceReader(data_dir / TRACE_SUBDIR)
    since, until = _window(session, messages)
    scan = reader.read_session(session.id, since=since, until=until, focus_turn_id=focus_turn_id)

    bodies = _bodies_by_turn(messages)
    focus = focus_turn_id or (str(scan.turns[-1].get("turn_id") or "") if scan.turns else None)
    # payload 有总量上限，先给点中的那一轮取：按文件顺序填会让最新那几轮全落到 skipped，
    # 而用户点的多半就是最新那一轮。
    states: dict[int, str] = {}
    for index in sorted(range(len(scan.turns)), key=lambda item: scan.turns[item].get("turn_id") != focus):
        states[index] = reader.resolve_payload(scan.turns[index])
    views = [_turn_view(record, payload_state=states[index]) for index, record in enumerate(scan.turns)]

    known = {view["turn_id"] for view in views}
    # 点中的那一轮没有 trace 记录、但正文还在库里时仍然画出来：说「这一轮没有记录」
    # 比给一片空白有用。
    if focus and focus not in known and focus in bodies:
        views.append(_missing_view(focus))
    for view in views:
        _attach_bodies(view, bodies.get(view["turn_id"], []))

    return {
        "session_id": session.id,
        "focus_turn_id": focus,
        "focus_found": bool(focus) and focus in known,
        "turns": views,
        "limits": {"max_turns": MAX_TURNS, "max_scan_lines": MAX_SCAN_LINES,
                   "max_day_files": MAX_DAY_FILES, "max_payload_bytes": MAX_PAYLOAD_BYTES,
                   "max_payload_total_bytes": MAX_PAYLOAD_TOTAL_BYTES,
                   "react_field_max_chars": REACT_FIELD_MAX_CHARS,
                   "react_turn_max_chars": REACT_TURN_MAX_CHARS},
        "scan": {"files": scan.files, "scanned_lines": scan.scanned_lines,
                 "scan_capped": scan.scan_capped, "turns_capped": scan.turns_capped,
                 "files_capped": scan.files_capped, "truncated": scan.truncated},
    }


def tool_body(*, messages: Sequence[Message], turn_id: str, call_id: str) -> dict:
    """按需取某一步的工具正文。turn_id 与 call_id 一起配对：只按 call_id 找，
    同一个会话里别的轮次会串过来。找不到就如实说没有，不要报错——
    按设计不落库的工具（artifact_read、MCP 等）走的就是这条路。"""
    for item in messages:
        if item.role != "tool" or (item.turn_id or "") != turn_id:
            continue
        entry = item.activity[0] if item.activity else {}
        if str(entry.get("call_id") or "") != call_id:
            continue
        return {"turn_id": turn_id, "call_id": call_id, "name": str(entry.get("name") or ""),
                "chars": len(item.content), "text": item.content, "found": True}
    return {"turn_id": turn_id, "call_id": call_id, "name": "", "chars": 0,
            "text": None, "found": False}


def _window(session: SessionSummary, messages: Sequence[Message]) -> tuple[str | None, str | None]:
    """挑日期文件用的时间窗，取这个会话的消息范围。

    一轮的 trace 记录与它的用户消息只差毫秒，所以读端两侧各放宽一天足够容下跨零点。
    """
    stamps = [item.created_at for item in messages if item.created_at]
    if stamps:
        return min(stamps), max(stamps)
    # 一条消息都没有（比如附件缺失，第一轮在写消息之前就失败了）：退到会话自己的时间。
    return session.updated_at or None, session.updated_at or None


def _turn_view(record: dict, *, payload_state: str) -> dict:
    tools = record.get("tools")
    return {
        "turn_id": str(record.get("turn_id") or ""),
        "trace_record": True,
        # 执行流程排在最前：这是打开侧栏第一眼要看的东西，统计与消耗跟在后面。
        "react": _react_view(record.get("react")),
        "started_at": record.get("started_at"),
        "status": record.get("status"),
        "error_code": record.get("error_code"),
        "duration_ms": record.get("duration_ms"),
        "scope_mode": record.get("scope_mode"),
        "prompt_version": record.get("prompt_version"),
        "answer_chars": record.get("answer_chars"),
        "citations": record.get("citations"),
        "citations_retrieved": record.get("citations_retrieved"),
        "tool_rounds": record.get("tool_rounds"),
        "resolution": record.get("resolution"),
        "responder": record.get("responder"),
        "usage": record.get("usage"),
        "payload_state": payload_state,
        "tools": [_span_view(index, span) for index, span in enumerate(tools)] if isinstance(tools, list) else [],
        "extras": {key: value for key, value in record.items() if key not in _CARRIED},
        "subagent_bodies": [],
        "unmatched_bodies": [],
    }


def _react_view(react: object) -> dict:
    """ReAct 时序。payload 被清理时正文只剩 ref，那时报 None 并留下原文长度——
    界面据此说「这段没取到」，而不是显示成空。"""
    fields = react if isinstance(react, dict) else {}
    steps = []
    for step in fields.get("steps") or []:
        source = step if isinstance(step, dict) else {}
        view = {name: source.get(name) for name in _STEP_FIELDS}
        for name in ("reasoning", "text"):
            if not isinstance(view[name], str):
                view[name] = None
            view[f"{name}_chars"] = int(source.get(f"{name}_chars") or 0)
        view["calls"] = list(view["calls"] or [])
        steps.append(view)
    answer = fields.get("answer")
    return {"steps": steps, "answer": answer if isinstance(answer, str) else None,
            "answer_chars": int(fields.get("answer_chars") or 0),
            "subagents": list(fields.get("subagents") or []),
            "dropped_chars": int(fields.get("dropped_chars") or 0)}


def _missing_view(turn_id: str) -> dict:
    view = _turn_view({"turn_id": turn_id}, payload_state="inline")
    view["trace_record"] = False
    return view


def _span_view(index: int, span: object) -> dict:
    view: dict = {"index": index}
    fields = span if isinstance(span, dict) else {}
    view.update({name: fields.get(name) for name in _SPAN_FIELDS})
    # 精确配对靠它。视图里不带出来，按 call_id 那条路整条静默失效，退回按工具名先来先接。
    view["call_id"] = str(fields.get("call_id") or "")
    raw = fields.get("arguments")
    if isinstance(raw, dict) and "payload_ref" in raw:
        # payload 被清理或超限时留在这里：参数不是空的，只是这次没取到。
        view["arguments"], view["arguments_ref"] = None, {"chars": raw.get("chars")}
    else:
        view["arguments"], view["arguments_ref"] = raw, None
    view["body"] = None
    view["body_state"] = _body_state(fields)
    return view


def _body_state(span: dict) -> str:
    """这一步为什么没有正文。空着看起来像出了错，四种原因各有各的说法。"""
    if span.get("reused"):
        return "reused"
    if span.get("decision") == "denied":
        return "denied"
    if span.get("ok") is False:
        return "failed"
    if str(span.get("name") or "") not in PERSISTED_TOOL_BODIES:
        return "not_persisted"
    return "missing"


def _bodies_by_turn(messages: Sequence[Message]) -> dict[str, list[dict]]:
    """只取正文的元数据。正文本身走 tool_body()，点开哪一步才拉哪一条。"""
    grouped: dict[str, list[dict]] = {}
    for item in messages:
        if item.role != "tool":
            continue
        entry = item.activity[0] if item.activity else {}
        grouped.setdefault(item.turn_id or "", []).append({
            "call_id": str(entry.get("call_id") or ""),
            "name": str(entry.get("name") or ""),
            "chars": len(item.content),
        })
    return grouped


def _can_have_body(span: dict) -> bool:
    """这一步会不会留下正文。复用的那次直接拿上一次的结果，落库那一步整个跳过；
    失败与被拦下的也不落。不先排掉它们，按顺序接就会把后面那次的正文接到这里，
    界面上就成了「查 A 拿回了 B 的结果」。"""
    return not span.get("reused") and span.get("ok") is not False and span.get("decision") != "denied"


def _attach_bodies(view: dict, bodies: list[dict]) -> None:
    """把正文接到对应的 span 上。

    优先按 call_id 精确认；老 trace 的 span 没记这个字段，那些退回按工具名先来先接。
    子任务的正文（call_id 带 sub: 前缀）父轮根本没有对应的 span，单列一栏——
    硬接到某个 span 底下会让人以为那次调用取回的是子任务查到的东西。
    """
    by_id = {tool["call_id"]: tool for tool in view["tools"] if tool.get("call_id")}
    pending: dict[str, deque[dict]] = {}
    for body in bodies:
        if is_subagent_call(body["call_id"]):
            view["subagent_bodies"].append(body)
            continue
        exact = by_id.get(body["call_id"])
        if exact is not None and exact["body"] is None:
            exact["body"] = body
            continue
        if is_seed_call(body["call_id"]):
            seed = next((tool for tool in view["tools"] if tool.get("origin") == "seed"), None)
            if seed is not None and seed["body"] is None:
                seed["body"] = body
                continue
        pending.setdefault(body["name"], deque()).append(body)
    for tool in view["tools"]:
        queue = pending.get(tool.get("name") or "")
        if tool["body"] is None and queue and _can_have_body(tool):
            tool["body"] = queue.popleft()
    for tool in view["tools"]:
        if tool["body"] is not None:
            tool["body_state"] = "stored"
    view["unmatched_bodies"] = [body for queue in pending.values() for body in queue]
