from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_shift(seconds: float) -> str:
    """相对当前时间偏移的时间戳，与 utc_now 同格式，可直接参与字符串比较。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
