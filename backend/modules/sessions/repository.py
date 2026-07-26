from __future__ import annotations
import json
import sqlite3
from core.store import SQLiteStore
def _purge_sessions(connection: sqlite3.Connection, session_ids: list[str]) -> None:
    """引用会话的表全是 NO ACTION 外键，删除顺序得自己排：引用方先走，被引用方后走。
    session_compactions 还引用 messages，所以必须排在 messages 之前。"""
    if not session_ids: return
    marks = ",".join("?" * len(session_ids))
    for statement in (
        f"DELETE FROM session_compactions WHERE session_id IN ({marks})",
        f"DELETE FROM turn_course_context WHERE turn_id IN (SELECT id FROM turn_requests WHERE session_id IN ({marks}))",
        f"DELETE FROM turn_requests WHERE session_id IN ({marks})",
        f"DELETE FROM messages WHERE session_id IN ({marks})",
        f"DELETE FROM attachments WHERE session_id IN ({marks})",
        f"DELETE FROM artifacts WHERE session_id IN ({marks})",
        # 渠道绑定只是"当前聊到哪个会话"的指针，会话没了指针清空即可。
        f"UPDATE channel_bindings SET active_session_id = NULL WHERE active_session_id IN ({marks})",
        f"DELETE FROM sessions WHERE id IN ({marks})",
    ): connection.execute(statement, session_ids)
class SessionRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def delete_session(self, session_id: str) -> bool:
        with self._store.write() as c:
            if c.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone() is None: return False
            _purge_sessions(c, [session_id]); return True
    def delete_course_sessions(self, connection: sqlite3.Connection, *, course_id: str) -> None:
        """随课程一起删掉它的课程会话，与调用方共用事务；通用会话只清掉历史解析痕迹。"""
        _purge_sessions(connection, [row["id"] for row in connection.execute("SELECT id FROM sessions WHERE course_id = ?", (course_id,))])
        connection.execute("UPDATE sessions SET last_resolved_course_id = NULL WHERE last_resolved_course_id = ?", (course_id,))
    def list_session_rows(self, *, scope_mode: str | None, course_id: str | None):
        clauses, params = ["kind = 'user'"], []
        if scope_mode: clauses.append("scope_mode = ?"); params.append(scope_mode)
        if course_id: clauses.append("course_id = ?"); params.append(course_id)
        with self._store.read() as c: return c.execute(f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC", params).fetchall()
    def get_session_row(self, session_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    def insert_session(self, *, session_id: str, title: str, scope_mode: str, course_id: str | None, source: str, owner_id: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("INSERT INTO sessions(id, title, scope_mode, course_id, source, owner_id, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?)", (session_id, title, scope_mode, course_id, source, owner_id, timestamp, timestamp))
    def list_message_rows(self, session_id: str):
        with self._store.read() as c: return c.execute("SELECT m.*, ctx.resolution_status, ctx.resolved_course_id, ctx.reason AS resolution_reason FROM messages m LEFT JOIN turn_course_context ctx ON ctx.turn_id = m.turn_id WHERE m.session_id = ? ORDER BY m.created_at ASC, m.rowid ASC", (session_id,)).fetchall()
    def insert_message(self, *, message_id: str, session_id: str, turn_id: str | None, role: str, content: str, citations: list[dict], status: str, timestamp: str, activity: list[dict] | None = None) -> None:
        with self._store.write() as c:
            c.execute("INSERT INTO messages(id, session_id, turn_id, role, content, citations_json, status, created_at, activity_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (message_id, session_id, turn_id, role, content, json.dumps(citations, ensure_ascii=False), status, timestamp, json.dumps(activity or [], ensure_ascii=False))); c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))
    def get_or_create_turn(self, *, turn_id: str, session_id: str, client_request_id: str, timestamp: str, stale_before: str):
        with self._store.write() as c:
            existing = c.execute("SELECT * FROM turn_requests WHERE session_id = ? AND client_request_id = ?", (session_id, client_request_id)).fetchone()
            if existing: return existing, False
            # 心跳早于阈值的 running turn 已经失活（客户端断开后生成器挂住），落为 failed 让本轮接管。
            c.execute("UPDATE turn_requests SET status = 'failed', completed_at = ? WHERE session_id = ? AND status = 'running' AND COALESCE(heartbeat_at, created_at) < ?", (timestamp, session_id, stale_before))
            c.execute("INSERT INTO turn_requests(id, session_id, client_request_id, status, created_at, heartbeat_at) VALUES (?, ?, ?, 'running', ?, ?)", (turn_id, session_id, client_request_id, timestamp, timestamp)); return c.execute("SELECT * FROM turn_requests WHERE id = ?", (turn_id,)).fetchone(), True
    def touch_turn(self, *, turn_id: str, timestamp: str) -> bool:
        """续约并回答"这一轮是否仍持有会话"：被抢占的 turn 已不是 running，更新不到行。"""
        with self._store.write() as c: return c.execute("UPDATE turn_requests SET heartbeat_at = ? WHERE id = ? AND status = 'running'", (timestamp, turn_id)).rowcount > 0
    def save_course_context(self, *, turn_id: str, resolution_status: str, resolved_course_id: str | None, resolver_version: str, reason: str, timestamp: str) -> None:
        # 纯 INSERT：turn_course_context 是不可变记录，重复写入应当报错而不是覆盖。
        with self._store.write() as c: c.execute("INSERT INTO turn_course_context(turn_id, resolution_status, resolved_course_id, resolver_version, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", (turn_id, resolution_status, resolved_course_id, resolver_version, reason, timestamp))
    def set_title(self, *, session_id: str, title: str, timestamp: str) -> bool:
        with self._store.write() as c: return c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, timestamp, session_id)).rowcount > 0
    def set_title_if_default(self, *, session_id: str, title: str, default: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND title = ?", (title, timestamp, session_id, default))
    def update_last_resolved_course(self, *, session_id: str, course_id: str | None) -> None:
        if course_id is not None:
            with self._store.write() as c: c.execute("UPDATE sessions SET last_resolved_course_id = ? WHERE id = ?", (course_id, session_id))
    def insert_attachment(self, *, attachment_id: str, session_id: str, filename: str, mime_type: str, byte_size: int, width: int, height: int, transcription: str, needs_confirmation: bool, provider: str, model: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("INSERT INTO attachments(id, session_id, filename, mime_type, byte_size, width, height, transcription, needs_confirmation, provider, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (attachment_id, session_id, filename, mime_type, byte_size, width, height, transcription, int(needs_confirmation), provider, model, timestamp))
    def get_attachment_rows(self, *, session_id: str, attachment_ids: list[str]):
        placeholders = ",".join("?" for _ in attachment_ids)
        with self._store.read() as c: return c.execute(f"SELECT * FROM attachments WHERE session_id = ? AND id IN ({placeholders}) ORDER BY created_at ASC", [session_id, *attachment_ids]).fetchall()
    def finish_turn(self, *, turn_id: str, status: str, timestamp: str) -> None:
        # 只收尾仍在 running 的 turn：已被抢占的失活 turn 不该改写终态。
        with self._store.write() as c: c.execute("UPDATE turn_requests SET status = ?, completed_at = ? WHERE id = ? AND status = 'running'", (status, timestamp, turn_id))
    def fail_running_turns(self, *, timestamp: str) -> int:
        with self._store.write() as c: return c.execute("UPDATE turn_requests SET status = 'failed', completed_at = ? WHERE status = 'running'", (timestamp,)).rowcount
