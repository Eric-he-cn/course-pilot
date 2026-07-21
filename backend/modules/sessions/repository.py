from __future__ import annotations
import json
import sqlite3
from app.store import SQLiteStore
class SessionRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def list_session_rows(self, *, scope_mode: str | None, course_id: str | None):
        clauses, params = ["kind = 'user'"], []
        if scope_mode: clauses.append("scope_mode = ?"); params.append(scope_mode)
        if course_id: clauses.append("course_id = ?"); params.append(course_id)
        with self._store.read() as c: return c.execute(f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC", params).fetchall()
    def get_session_row(self, session_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    def get_source_session_row(self, *, source: str, scope_mode: str, owner_id: str):
        with self._store.read() as c: return c.execute("SELECT * FROM sessions WHERE source = ? AND scope_mode = ? AND owner_id = ? AND kind = 'user' ORDER BY created_at ASC LIMIT 1", (source, scope_mode, owner_id)).fetchone()
    def insert_session(self, *, session_id: str, title: str, scope_mode: str, course_id: str | None, source: str, owner_id: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("INSERT INTO sessions(id, title, scope_mode, course_id, source, owner_id, kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?)", (session_id, title, scope_mode, course_id, source, owner_id, timestamp, timestamp))
    def list_message_rows(self, session_id: str):
        with self._store.read() as c: return c.execute("SELECT m.*, ctx.resolution_status, ctx.resolved_course_id, ctx.reason AS resolution_reason FROM messages m LEFT JOIN turn_course_context ctx ON ctx.turn_id = m.turn_id WHERE m.session_id = ? ORDER BY m.created_at ASC, m.rowid ASC", (session_id,)).fetchall()
    def insert_message(self, *, message_id: str, session_id: str, turn_id: str | None, role: str, content: str, citations: list[dict], status: str, timestamp: str) -> None:
        with self._store.write() as c:
            c.execute("INSERT INTO messages(id, session_id, turn_id, role, content, citations_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (message_id, session_id, turn_id, role, content, json.dumps(citations, ensure_ascii=False), status, timestamp)); c.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id))
    def get_or_create_turn(self, *, turn_id: str, session_id: str, client_request_id: str, timestamp: str):
        with self._store.write() as c:
            existing = c.execute("SELECT * FROM turn_requests WHERE session_id = ? AND client_request_id = ?", (session_id, client_request_id)).fetchone()
            if existing: return existing, False
            c.execute("INSERT INTO turn_requests(id, session_id, client_request_id, status, created_at) VALUES (?, ?, ?, 'running', ?)", (turn_id, session_id, client_request_id, timestamp)); return c.execute("SELECT * FROM turn_requests WHERE id = ?", (turn_id,)).fetchone(), True
    def save_course_context(self, *, turn_id: str, resolution_status: str, resolved_course_id: str | None, resolver_version: str, reason: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("INSERT OR REPLACE INTO turn_course_context(turn_id, resolution_status, resolved_course_id, resolver_version, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", (turn_id, resolution_status, resolved_course_id, resolver_version, reason, timestamp))
    def update_last_resolved_course(self, *, session_id: str, course_id: str | None) -> None:
        if course_id is not None:
            with self._store.write() as c: c.execute("UPDATE sessions SET last_resolved_course_id = ? WHERE id = ?", (course_id, session_id))
    def finish_turn(self, *, turn_id: str, status: str, timestamp: str) -> None:
        with self._store.write() as c: c.execute("UPDATE turn_requests SET status = ?, completed_at = ? WHERE id = ?", (status, timestamp, turn_id))
