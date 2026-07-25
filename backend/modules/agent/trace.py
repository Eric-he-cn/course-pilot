from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

# 超过这个长度的字段搬进 payload 文件，主 JSONL 只留 ref（架构 §16.1）。
_INLINE_MAX_CHARS = 200
_PAYLOAD_FIELDS = ("arguments",)


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
            print(f"trace write failed: {error}", file=sys.stderr)

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
