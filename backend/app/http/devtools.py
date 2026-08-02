"""开发者模式的 trace 视图：把一轮的时序和它取回的正文合成一份。

两个来源缺一不可：trace 给时序、耗时、参数与 usage，messages 表 role='tool' 的行给
工具取回的正文（trace 里只有「命中 6 段」这样的摘要）。

这条链路只服务观测。trace 写失败只打 warning、payload 可以被单独清理、整个目录也能删，
所以业务功能不该拿它当数据源——这里读不到就如实报空，不要在这里补数据。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from modules.agent.trace import (MAX_DAY_FILES, MAX_PAYLOAD_BYTES, MAX_PAYLOAD_TOTAL_BYTES,
                                 MAX_SCAN_LINES, MAX_TURNS, TRACE_SUBDIR, TraceReader,
                                 is_seed_call, is_subagent_call)
from modules.sessions.api import Message, SessionSummary

__all__ = ["MAX_BODY_CHARS", "MAX_DAY_FILES", "MAX_PAYLOAD_BYTES", "MAX_PAYLOAD_TOTAL_BYTES",
           "MAX_SCAN_LINES", "MAX_TURNS", "TRACE_SUBDIR", "build_trace_view"]

# 一次响应里工具正文的总量上限。单条上限 8000 字符，几十轮检索加起来能有几兆。
# 点中的那一轮先填，剩下的从新往旧填；被裁掉的保留 chars，标明它原本多长。
MAX_BODY_CHARS = 80_000

# 已经有专门字段承载的键。其余的原样进 extras——trace 的字段随功能增加，
# 读端按白名单挑会静默丢掉新加的那些。
_CARRIED = frozenset({
    "kind", "session_id", "turn_id", "started_at", "status", "error_code", "duration_ms",
    "scope_mode", "prompt_version", "answer_chars", "citations", "citations_retrieved",
    "tool_rounds", "resolution", "responder", "usage", "tools", "payload_ref",
})
_SPAN_FIELDS = ("origin", "name", "ok", "summary", "summary_key", "summary_args",
                "duration_ms", "decision", "reason", "reused")


def build_trace_view(*, session: SessionSummary, messages: Sequence[Message],
                     data_dir: Path, focus_turn_id: str | None) -> dict:
    reader = TraceReader(data_dir / TRACE_SUBDIR)
    since, until = _window(session, messages)
    scan = reader.read_session(session.id, since=since, until=until, focus_turn_id=focus_turn_id)

    views: list[dict] = []
    for record in scan.turns:
        views.append(_turn_view(record, payload_state=reader.resolve_payload(record)))

    bodies = _bodies_by_turn(messages)
    focus = focus_turn_id or (views[-1]["turn_id"] if views else None)
    known = {view["turn_id"] for view in views}
    # 点中的那一轮没有 trace 记录、但正文还在库里时仍然画出来：说「这一轮没有记录」
    # 比给一片空白有用。
    if focus and focus not in known and focus in bodies:
        views.append(_missing_view(focus))
    for view in views:
        _attach_bodies(view, bodies.get(view["turn_id"], []))
    _apply_body_budget(views, focus_turn_id=focus)

    return {
        "session_id": session.id,
        "focus_turn_id": focus,
        "focus_found": bool(focus) and focus in known,
        "turns": views,
        "limits": {"max_turns": MAX_TURNS, "max_scan_lines": MAX_SCAN_LINES,
                   "max_day_files": MAX_DAY_FILES, "max_body_chars": MAX_BODY_CHARS,
                   "max_payload_bytes": MAX_PAYLOAD_BYTES,
                   "max_payload_total_bytes": MAX_PAYLOAD_TOTAL_BYTES},
        "scan": {"files": scan.files, "scanned_lines": scan.scanned_lines,
                 "scan_capped": scan.scan_capped, "turns_capped": scan.turns_capped,
                 "files_capped": scan.files_capped, "truncated": scan.truncated},
    }


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
        "bodies_omitted": 0,
    }


def _missing_view(turn_id: str) -> dict:
    view = _turn_view({"turn_id": turn_id}, payload_state="inline")
    view["trace_record"] = False
    return view


def _span_view(index: int, span: object) -> dict:
    view: dict = {"index": index}
    fields = span if isinstance(span, dict) else {}
    view.update({name: fields.get(name) for name in _SPAN_FIELDS})
    raw = fields.get("arguments")
    if isinstance(raw, dict) and "payload_ref" in raw:
        # payload 被清理或超限时留在这里：参数不是空的，只是这次没取到。
        view["arguments"], view["arguments_ref"] = None, {"chars": raw.get("chars")}
    else:
        view["arguments"], view["arguments_ref"] = raw, None
    view["body"] = None
    return view


def _bodies_by_turn(messages: Sequence[Message]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in messages:
        if item.role != "tool":
            continue
        entry = item.activity[0] if item.activity else {}
        grouped.setdefault(item.turn_id or "", []).append({
            "call_id": str(entry.get("call_id") or ""),
            "name": str(entry.get("name") or ""),
            "chars": len(item.content),
            "text": item.content,
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
    view["unmatched_bodies"] = [body for queue in pending.values() for body in queue]


def _all_bodies(view: dict) -> Iterator[dict]:
    for tool in view["tools"]:
        if tool["body"] is not None:
            yield tool["body"]
    yield from view["subagent_bodies"]
    yield from view["unmatched_bodies"]


def _apply_body_budget(views: Iterable[dict], *, focus_turn_id: str | None) -> None:
    ordered = list(views)
    priority = ([view for view in ordered if view["turn_id"] == focus_turn_id]
                + [view for view in reversed(ordered) if view["turn_id"] != focus_turn_id])
    remaining = MAX_BODY_CHARS
    for view in priority:
        for body in _all_bodies(view):
            text = body["text"]
            if text is None:
                continue
            if len(text) <= remaining:
                remaining -= len(text)
            else:
                body["text"] = None
                view["bodies_omitted"] += 1
