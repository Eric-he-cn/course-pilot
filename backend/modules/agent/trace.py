from __future__ import annotations

import logging
import json
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .context import SEED_CALL_ID

logger = logging.getLogger(__name__)

# 超过这个长度的字段搬进 payload 文件，主 JSONL 只留 ref（架构 §16.1）。
_INLINE_MAX_CHARS = 200
_PAYLOAD_FIELDS = ("arguments",)

# 回读上限。trace 按天分文件、一天可以有几万行，所以三个维度都要有闸：
# 翻几个文件、扫多少行、返回多少轮。限制了覆盖就要在响应里说出来。
MAX_TURNS = 50
MAX_SCAN_LINES = 20_000
MAX_DAY_FILES = 40
# 单个 payload 文件的上限：写端只搬 arguments，正常是几 KB，超出这个数不读。
MAX_PAYLOAD_BYTES = 512 * 1024
# 一次回读读进来的 payload 总量。单文件上限乘以轮数会到几十兆，浏览器扛不住。
MAX_PAYLOAD_TOTAL_BYTES = 4 * 1024 * 1024

_DAY_NAME = re.compile(r"\d{4}-\d{2}-\d{2}")
# trace 在工作区里的子目录名。同一个名字也出现在 bootstrap 的装配处与工作区搬迁清单里。
TRACE_SUBDIR = "traces"


class TraceWriter:
    """每轮对话一条 JSONL 记录（含工具子 span），供离线评测与回放使用。

    主 JSONL 只存索引与摘要；原文（问题、检索参数等）落 trace_payloads/，通过
    payload_ref 关联，因此可以单独设置保留周期或一键删除，不必动索引本身。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._payloads = directory / "payloads"
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        # trace 是旁路观测，写失败不能影响对话本身。
        try:
            day = str(record.get("started_at", ""))[:10] or "unknown"
            turn_id = str(record.get("turn_id") or "unknown")
            index, payload = self._split(record, turn_id=turn_id)
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True)
                if payload:
                    self._payloads.mkdir(parents=True, exist_ok=True)
                    (self._payloads / f"{turn_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
                    index["payload_ref"] = f"payloads/{turn_id}.json"
                with (self._directory / f"{day}.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(index, ensure_ascii=False) + "\n")
        except Exception as error:
            logger.warning("trace 写入失败：%s", error)

    @staticmethod
    def _split(record: dict, *, turn_id: str) -> tuple[dict, dict]:
        """把长字段搬出索引；返回 (索引记录, payload)。"""
        index = dict(record)
        payload: dict = {}
        tools = index.get("tools")
        if isinstance(tools, list):
            trimmed = []
            for position, span in enumerate(tools):
                if not isinstance(span, dict):
                    trimmed.append(span)
                    continue
                span_copy = dict(span)
                for field in _PAYLOAD_FIELDS:
                    value = span_copy.get(field)
                    encoded = json.dumps(value, ensure_ascii=False) if value is not None else ""
                    if len(encoded) > _INLINE_MAX_CHARS:
                        payload.setdefault("tools", {})[f"{position}.{field}"] = value
                        span_copy[field] = {"payload_ref": f"tools.{position}.{field}", "chars": len(encoded)}
                trimmed.append(span_copy)
            index["tools"] = trimmed
        if payload:
            payload["turn_id"] = turn_id
        return index, payload


def _shift_day(day: str, days: int) -> str:
    try:
        return (date.fromisoformat(day) + timedelta(days=days)).isoformat()
    except ValueError:
        return day


@dataclass
class TraceScan:
    """一次回读的结果与它的覆盖范围。上限撞到了必须能报出来，否则少读的部分看不出来。"""
    turns: list[dict] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    scanned_lines: int = 0
    scan_capped: bool = False
    turns_capped: bool = False
    files_capped: bool = False

    @property
    def truncated(self) -> bool:
        return self.scan_capped or self.turns_capped or self.files_capped


class TraceReader:
    """按会话回读 TraceWriter 写下的 JSONL，供开发者模式这类观测用途。

    只做观测：trace 写失败只打 warning、payload 可以被单独清理、整个目录也可以删，
    所以任何业务功能都不该拿它当数据源——读不到就如实报空，不要在这里补数据。
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._payloads = directory / "payloads"
        # 一次请求一个 reader，所以余额跟着实例走
        self._payload_budget = MAX_PAYLOAD_TOTAL_BYTES

    def read_session(self, session_id: str, *, since: str | None = None, until: str | None = None,
                     focus_turn_id: str | None = None) -> TraceScan:
        """取这个会话的轮次，按 started_at 升序。

        since/until 是会话自己的时间窗（取自它的消息），用来挑日期文件：不挑的话，
        一个上月的会话要把这之后每天的文件都翻一遍。点中的那一轮预留一个名额，
        免得条数上限正好把用户点的那条挤掉。
        """
        scan = TraceScan()
        paths, scan.files_capped = self._day_files(since, until)
        reserved = 1 if focus_turn_id else 0
        focus: dict | None = None
        for path in paths:
            if len(scan.turns) >= MAX_TURNS - reserved or scan.scan_capped:
                scan.turns_capped = scan.turns_capped or len(scan.turns) >= MAX_TURNS - reserved
                break
            scan.files.append(path.name)
            room = MAX_TURNS - reserved - len(scan.turns)
            window: deque[dict] = deque(maxlen=room)
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        scan.scanned_lines += 1
                        if scan.scanned_lines > MAX_SCAN_LINES:
                            scan.scanned_lines = MAX_SCAN_LINES
                            scan.scan_capped = True
                            break
                        # 便宜的预筛：同一个文件里绝大多数行属于别的会话，
                        # 每行都 json.loads 是这条路径上最贵的一步。
                        if session_id not in line:
                            continue
                        record = self._parse(line)
                        if record is None or record.get("session_id") != session_id:
                            continue
                        if len(window) == window.maxlen:
                            scan.turns_capped = True
                        window.append(record)
                        if focus_turn_id and record.get("turn_id") == focus_turn_id:
                            focus = record
            except OSError as error:
                logger.warning("trace 读取失败 %s：%s", path.name, error)
                continue
            # 日期文件从新往旧翻，所以先读到的那批在时间上更晚，接在前面。
            scan.turns = list(window) + scan.turns
        if focus is not None and not any(item.get("turn_id") == focus_turn_id for item in scan.turns):
            scan.turns.append(focus)
        scan.turns.sort(key=lambda item: str(item.get("started_at") or ""))
        return scan

    def resolve_payload(self, record: dict) -> str:
        """把搬进 payloads/ 的长字段拼回 tools[]（原地改 record）。返回状态供界面区分空态。"""
        ref = record.get("payload_ref")
        if not ref:
            return "inline"
        try:
            resolved = (self._directory / str(ref)).resolve()
            resolved.relative_to(self._payloads.resolve())
        except (OSError, ValueError):
            return "invalid"
        try:
            size = resolved.stat().st_size
            if size > MAX_PAYLOAD_BYTES:
                return "oversized"
            if size > self._payload_budget:
                return "skipped"
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            self._payload_budget -= size
        except OSError:
            # payload 可以单独清理，索引里就只剩 ref 了。
            return "missing"
        except (json.JSONDecodeError, ValueError):
            return "invalid"
        slots = payload.get("tools") if isinstance(payload, dict) else None
        if not isinstance(slots, dict):
            return "invalid"
        tools = record.get("tools")
        if isinstance(tools, list):
            for position, span in enumerate(tools):
                if not isinstance(span, dict):
                    continue
                for name in _PAYLOAD_FIELDS:
                    key = f"{position}.{name}"
                    if key in slots and isinstance(span.get(name), dict) and "payload_ref" in span[name]:
                        span[name] = slots[key]
        return "resolved"

    def _day_files(self, since: str | None, until: str | None) -> tuple[list[Path], bool]:
        if not self._directory.is_dir():
            return [], False
        low = _shift_day(since[:10], -1) if since else None
        high = _shift_day(until[:10], 1) if until else None

        def in_window(path: Path) -> bool:
            stem = path.stem
            # 名字不是日期（unknown、被改过名的备份）时日期无从判断，一律保留。
            if not _DAY_NAME.fullmatch(stem):
                return True
            return (low is None or stem >= low) and (high is None or stem <= high)

        try:
            files = sorted((path for path in self._directory.glob("*.jsonl") if path.is_file() and in_window(path)),
                           key=lambda path: path.stem, reverse=True)
        except OSError as error:
            logger.warning("trace 目录读取失败：%s", error)
            return [], False
        return files[:MAX_DAY_FILES], len(files) > MAX_DAY_FILES

    @staticmethod
    def _parse(line: str) -> dict | None:
        # JSONL 是追加写的，进程被杀会留下半行；一条坏行不该让整份读失败。
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
        return record if isinstance(record, dict) else None


def is_subagent_call(call_id: str) -> bool:
    """子任务的工具正文落库时 call_id 带 sub: 前缀，父轮的 trace 里没有对应的 span。"""
    return str(call_id).startswith("sub:")


def is_seed_call(call_id: str) -> bool:
    return str(call_id) == SEED_CALL_ID
