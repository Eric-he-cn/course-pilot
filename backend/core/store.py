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
    (5, """
        ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local-web';
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
    # 概念目录：证据归因的 ID 真源。由教材索引后的确定性任务产出，可重放且不改已有 id。
    (12, """
        CREATE TABLE IF NOT EXISTS concepts (
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
            name TEXT NOT NULL, chapter TEXT, material_id TEXT REFERENCES materials(id),
            page INTEGER, mention_count INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_concept_name_per_course ON concepts(course_id, name);
        CREATE INDEX IF NOT EXISTS idx_concepts_course ON concepts(course_id, mention_count DESC);
        CREATE TABLE IF NOT EXISTS concept_aliases (
            concept_id TEXT NOT NULL REFERENCES concepts(id), alias TEXT NOT NULL,
            PRIMARY KEY (concept_id, alias)
        );
    """),
    # 练习等跨轮产物：envelope 由服务端硬校验，payload 由 skill 自行约定。
    (13, """
        CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
            session_id TEXT NOT NULL REFERENCES sessions(id), kind TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK(visibility IN ('user_visible', 'model_private')),
            payload_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_artifacts_course_kind ON artifacts(course_id, kind, created_at DESC);
    """),
    # 掌握度投影：唯一真源是 evidence_events，这张表可以随时从事件流全量重建。
    (14, """
        CREATE TABLE IF NOT EXISTS concept_mastery (
            concept_id TEXT PRIMARY KEY REFERENCES concepts(id),
            course_id TEXT NOT NULL REFERENCES courses(id),
            bkt_p REAL NOT NULL, fsrs_stability REAL NOT NULL, fsrs_difficulty REAL NOT NULL,
            objective_events INTEGER NOT NULL DEFAULT 0,
            last_reviewed_at TEXT, due_at TEXT,
            algorithm_version TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mastery_course ON concept_mastery(course_id, bkt_p);
        CREATE INDEX IF NOT EXISTS idx_mastery_due ON concept_mastery(course_id, due_at);
    """),
    # 计划每次改版留一条 diff：条目可被重写，改动记录不可覆盖。
    (15, """
        CREATE TABLE IF NOT EXISTS plan_revisions (
            id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id),
            version INTEGER NOT NULL, turn_id TEXT, note TEXT,
            diff_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_revision_version ON plan_revisions(plan_id, version);
    """),
    # 用户导入的 skill。allowed_tools 存原始声明，实际可用集合读时按白名单取交集。
    (16, """
        CREATE TABLE IF NOT EXISTS user_skills (
            name TEXT PRIMARY KEY, content_hash TEXT NOT NULL, source_text TEXT NOT NULL,
            description TEXT NOT NULL, when_to_use TEXT NOT NULL, allowed_tools_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('draft', 'enabled', 'permission_denied')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """),
    # 对话压缩：摘要 append-only，最新一条生效。水位用 created_at 而不是消息 id——
    # 消息 id 是无序 uuid，比不了先后，也做不了单调校验。
    (17, """
        CREATE TABLE IF NOT EXISTS session_compactions (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
            covers_through_message_id TEXT NOT NULL REFERENCES messages(id),
            covers_through_created_at TEXT NOT NULL, covers_message_count INTEGER NOT NULL,
            summary_text TEXT NOT NULL, prompt_version TEXT NOT NULL,
            turn_id TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_compaction_session ON session_compactions(session_id, created_at DESC);
    """),
    # 扫描版 PDF 的 OCR 批准。OCR 要花模型额度，所以要有一条「用户看过估算并同意」的记录；
    # 留在库里，重新索引就不必再问一次。
    (18, "ALTER TABLE materials ADD COLUMN ocr_approved INTEGER NOT NULL DEFAULT 0;"),
    # 默认的 unicode61 把整段中文当成一个 token，中文这一路的 BM25 等于没在工作。
    # trigram 按三字滑窗切，中英文都能命中；索引从 chunks 重建，不必重新切块或嵌入。
    (19, """
        DROP TABLE IF EXISTS chunks_fts;
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED, course_id UNINDEXED, content, tokenize='trigram'
        );
        INSERT INTO chunks_fts(chunk_id, course_id, content) SELECT id, course_id, content FROM chunks;
    """),
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
