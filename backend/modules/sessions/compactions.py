from __future__ import annotations

from dataclasses import dataclass

from core.common import new_id, utc_now
from core.store import SQLiteStore


@dataclass(frozen=True)
class ConversationSummary:
    summary_text: str
    covers_through_created_at: str
    covers_message_count: int
    prompt_version: str


class CompactionStore:
    """会话摘要：append-only，最新一条生效，旧的留着可审计。"""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def latest(self, *, session_id: str) -> ConversationSummary | None:
        with self._store.read() as connection:
            row = connection.execute(
                "SELECT summary_text, covers_through_created_at, covers_message_count, prompt_version"
                " FROM session_compactions WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationSummary(row["summary_text"], row["covers_through_created_at"], row["covers_message_count"], row["prompt_version"])

    def append(
        self, *, session_id: str, summary_text: str, covers_through_message_id: str,
        covers_through_created_at: str, covers_message_count: int, prompt_version: str, turn_id: str | None,
    ) -> bool:
        """水位只能前进：并发写入时较老的水位不覆盖较新的（否则部分内容会同时
        出现在摘要和原文里，白占上下文）。"""
        with self._store.write() as connection:
            current = connection.execute(
                "SELECT covers_through_created_at FROM session_compactions WHERE session_id = ?"
                " ORDER BY created_at DESC LIMIT 1", (session_id,),
            ).fetchone()
            if current is not None and covers_through_created_at <= current["covers_through_created_at"]:
                return False
            connection.execute(
                "INSERT INTO session_compactions(id, session_id, covers_through_message_id,"
                " covers_through_created_at, covers_message_count, summary_text, prompt_version, turn_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (new_id("compaction"), session_id, covers_through_message_id, covers_through_created_at,
                 covers_message_count, summary_text, prompt_version, turn_id, utc_now()),
            )
        return True
