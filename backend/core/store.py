from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, """
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS courses (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL, wiki_enabled INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, scope_mode TEXT NOT NULL CHECK(scope_mode IN ('general','course')), course_id TEXT REFERENCES courses(id), last_resolved_course_id TEXT REFERENCES courses(id), source TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, CHECK((scope_mode = 'general' AND course_id IS NULL) OR (scope_mode = 'course' AND course_id IS NOT NULL)));
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), role TEXT NOT NULL CHECK(role IN ('user','assistant','system')), turn_id TEXT, content TEXT NOT NULL, citations_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'complete', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_requests (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), client_request_id TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT, UNIQUE(session_id, client_request_id));
CREATE TABLE IF NOT EXISTS turn_course_context (turn_id TEXT PRIMARY KEY REFERENCES turn_requests(id), resolution_status TEXT NOT NULL, resolved_course_id TEXT REFERENCES courses(id), resolver_version TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS materials (id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id), filename TEXT NOT NULL, storage_path TEXT NOT NULL, mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL, index_status TEXT NOT NULL DEFAULT 'uploaded', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, type TEXT NOT NULL CHECK(type IN ('index','wiki')), material_id TEXT NOT NULL REFERENCES materials(id), course_id TEXT NOT NULL REFERENCES courses(id), status TEXT NOT NULL, stage TEXT NOT NULL, progress INTEGER NOT NULL, error_message TEXT, retrieval_backend TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES materials(id), course_id TEXT NOT NULL REFERENCES courses(id), ordinal INTEGER NOT NULL, page INTEGER, content TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC); CREATE INDEX IF NOT EXISTS idx_messages_session_created ON messages(session_id, created_at); CREATE INDEX IF NOT EXISTS idx_materials_course ON materials(course_id); CREATE INDEX IF NOT EXISTS idx_chunks_course ON chunks(course_id);
"""),
    (2, "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, course_id UNINDEXED, content);"),
    # These partial indexes make the local single-process policies durable if a
    # second server process is accidentally pointed at the same SQLite file.
    (3, "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_turn_per_session ON turn_requests(session_id) WHERE status = 'running';"),
    (4, "CREATE UNIQUE INDEX IF NOT EXISTS idx_feishu_general_session ON sessions(source) WHERE source = 'feishu' AND scope_mode = 'general' AND kind = 'user';"),
    (5, """
        DROP INDEX IF EXISTS idx_feishu_general_session;
        ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local-web';
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feishu_general_per_owner ON sessions(source, owner_id)
            WHERE source = 'feishu' AND scope_mode = 'general' AND kind = 'user';
        CREATE TABLE IF NOT EXISTS channel_bindings (
            provider TEXT NOT NULL, external_user_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            active_session_id TEXT REFERENCES sessions(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(provider, external_user_id)
        );
    """),
    (6, "ALTER TABLE turn_course_context ADD COLUMN created_at TEXT;"),
    # 学习档案与计划的存储骨架：读接口先行，写链路随掌握度/规划功能落地。
    (7, """
        CREATE TABLE IF NOT EXISTS evidence_events (
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
            concept_id TEXT, attribution_status TEXT NOT NULL DEFAULT 'unattributed',
            topic_hint TEXT, kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evidence_course_created ON evidence_events(course_id, created_at);
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
            status TEXT NOT NULL DEFAULT 'active', version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_active_plan_per_course ON plans(course_id) WHERE status = 'active';
        CREATE TABLE IF NOT EXISTS plan_items (
            id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id),
            due_date TEXT NOT NULL, title TEXT NOT NULL, concept_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_plan_items_plan ON plan_items(plan_id, due_date);
    """),
    # 语义检索：chunk 级 float32 向量，缺失时该 chunk 只参与词面检索。
    (8, "ALTER TABLE chunks ADD COLUMN embedding BLOB;"),
    # 会话图片附件：只保留转录与元数据，处理后的图片不落盘。
    (9, """
        CREATE TABLE IF NOT EXISTS attachments (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            filename TEXT NOT NULL, mime_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL,
            transcription TEXT NOT NULL, needs_confirmation INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id, created_at);
    """),
    # 本轮用了哪些工具：可解释性证据要跟着消息一起留存，刷新后仍能看到"查了什么"。
    (10, "ALTER TABLE messages ADD COLUMN activity_json TEXT;"),
    # turn 心跳：客户端断开后生成器可能一直挂着，靠心跳判定失活并让新一轮接管会话。
    (11, "ALTER TABLE turn_requests ADD COLUMN heartbeat_at TEXT;"),
)

class SQLiteStore:
    def __init__(self, path: Path) -> None: self.path, self._write_lock = path, threading.RLock()
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False); connection.row_factory = sqlite3.Row; connection.execute("PRAGMA foreign_keys=ON"); connection.execute("PRAGMA busy_timeout=5000"); return connection
    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL"); connection.execute("PRAGMA foreign_keys=ON"); connection.execute("PRAGMA busy_timeout=5000"); connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
            for version, sql in MIGRATIONS:
                if version not in applied: connection.executescript(sql); connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))
            connection.commit()
    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try: yield connection
        finally: connection.close()
    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self._connect()
            try: yield connection; connection.commit()
            except Exception: connection.rollback(); raise
            finally: connection.close()
