from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_shift(seconds: float) -> str:
    """相对当前时间偏移的时间戳，与 utc_now 同格式，可直接参与字符串比较。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def write_text_atomic(path: Path, content: str) -> None:
    """先写同目录的临时文件再改名。给「重写整份文件」用，尤其是先读后写的那几处。

    直接 write_text 会先清空再写，中途崩掉就只剩半份甚至 0 字节，而记忆与知识页手写区
    没有第二份副本。改名在同一文件系统内是原子的：要么旧内容还在，要么新内容整份就位。
    临时文件带 pid，避免两个进程同时写同一个路径时互相截断。
    挡的是进程崩溃，不挡断电——断电要 fsync，本地个人应用不值这个代价。
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
