from __future__ import annotations

import json
import sys
import threading
from pathlib import Path


class TraceWriter:
    """每轮对话一条 JSONL 记录（含工具子 span），供离线评测与回放使用。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        # trace 是旁路观测，写失败不能影响对话本身。
        try:
            day = str(record.get("started_at", ""))[:10] or "unknown"
            path = self._directory / f"{day}.jsonl"
            with self._lock:
                self._directory.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as error:
            print(f"trace write failed: {error}", file=sys.stderr)
