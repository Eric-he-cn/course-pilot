"""
【模块说明】
- 主要作用：基于 SQLite 持久化存储情景记忆（episodes）与用户画像（user_profiles）。
- 核心类：SQLiteMemoryStore。
- 核心方法：save_episode、search_episodes、get_profile、upsert_profile。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import sqlite3
import json
import uuid
import os
from datetime import datetime
from typing import List, Dict, Any, Optional


class SQLiteMemoryStore:
    """两张表：episodes（情景记忆）和 user_profiles（用户画像/语义记忆）。"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.getenv("MEMORY_DB_PATH", "./data/memory/memory.db")
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.db_path = db_path
        self.search_backend = str(os.getenv("MEMORY_SEARCH_BACKEND", "fts5")).strip().lower() or "fts5"
        self._init_tables()

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_tables(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id          TEXT PRIMARY KEY,
                    user_id     TEXT NOT NULL DEFAULT 'default',
                    course_name TEXT NOT NULL,
                    event_type  TEXT NOT NULL,    -- 'qa' | 'mistake' | 'practice' | 'exam'
                    content     TEXT NOT NULL,    -- 问题(+答案摘要)的自然语言描述
                    importance  REAL DEFAULT 0.5, -- 0~1，错题=0.9，普通问答=0.5
                    created_at  TEXT NOT NULL,
                    last_accessed_at TEXT NOT NULL,
                    metadata    TEXT DEFAULT '{}'  -- JSON: score, tags, doc_ids, etc.
                );

                CREATE INDEX IF NOT EXISTS idx_ep_course
                    ON episodes(user_id, course_name, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ep_accessed
                    ON episodes(user_id, course_name, last_accessed_at DESC);

                CREATE INDEX IF NOT EXISTS idx_ep_type
                    ON episodes(user_id, course_name, event_type);

                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id     TEXT NOT NULL,
                    course_name TEXT NOT NULL,
                    weak_points TEXT DEFAULT '[]',   -- JSON list of str
                    concept_mastery TEXT DEFAULT '{}', -- JSON dict: concept -> {mastery, attempts, avg_score}
                    preference_items TEXT DEFAULT '[]', -- JSON list: {text, source, updated_at}
                    pref_style  TEXT DEFAULT 'step_by_step',
                    total_qa    INTEGER DEFAULT 0,
                    total_practice INTEGER DEFAULT 0,
                    avg_score   REAL DEFAULT 0.0,
                    updated_at  TEXT,
                    PRIMARY KEY (user_id, course_name)
                );
            """)
            # 兼容历史数据库：若旧表不存在 concept_mastery 列，自动补齐。
            cols = conn.execute("PRAGMA table_info(user_profiles)").fetchall()
            col_names = {r[1] for r in cols}
            if "concept_mastery" not in col_names:
                conn.execute(
                    "ALTER TABLE user_profiles ADD COLUMN concept_mastery TEXT DEFAULT '{}'"
                )
            if "preference_items" not in col_names:
                conn.execute(
                    "ALTER TABLE user_profiles ADD COLUMN preference_items TEXT DEFAULT '[]'"
                )
            ep_cols = conn.execute("PRAGMA table_info(episodes)").fetchall()
            ep_col_names = {r[1] for r in ep_cols}
            if "last_accessed_at" not in ep_col_names:
                conn.execute(
                    "ALTER TABLE episodes ADD COLUMN last_accessed_at TEXT DEFAULT ''"
                )
                conn.execute(
                    """
                    UPDATE episodes
                    SET last_accessed_at = CASE
                        WHEN last_accessed_at IS NULL OR last_accessed_at = '' THEN created_at
                        ELSE last_accessed_at
                    END
                    """
                )
            self._init_fts(conn)

    def _init_fts(self, conn: sqlite3.Connection) -> None:
        """初始化 FTS5 虚表与触发器；SQLite 不支持 FTS5 时静默回退。"""
        try:
            conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
                    id UNINDEXED,
                    user_id UNINDEXED,
                    course_name UNINDEXED,
                    event_type UNINDEXED,
                    content
                );

                CREATE TRIGGER IF NOT EXISTS trg_episodes_ai AFTER INSERT ON episodes BEGIN
                    INSERT INTO episodes_fts(id, user_id, course_name, event_type, content)
                    VALUES (new.id, new.user_id, new.course_name, new.event_type, new.content);
                END;

                CREATE TRIGGER IF NOT EXISTS trg_episodes_ad AFTER DELETE ON episodes BEGIN
                    DELETE FROM episodes_fts WHERE id = old.id;
                END;

                CREATE TRIGGER IF NOT EXISTS trg_episodes_au AFTER UPDATE ON episodes BEGIN
                    DELETE FROM episodes_fts WHERE id = old.id;
                    INSERT INTO episodes_fts(id, user_id, course_name, event_type, content)
                    VALUES (new.id, new.user_id, new.course_name, new.event_type, new.content);
                END;
            """)
            conn.execute(
                """
                INSERT INTO episodes_fts(id, user_id, course_name, event_type, content)
                SELECT e.id, e.user_id, e.course_name, e.event_type, e.content
                FROM episodes e
                WHERE NOT EXISTS (
                    SELECT 1 FROM episodes_fts f WHERE f.id = e.id
                )
                """
            )
        except Exception:
            # 兼容不带 FTS5 的 SQLite 构建：保留 LIKE 路径。
            pass

    # ── 情景记忆 CRUD ─────────────────────────────────────────────────────────

    def save_episode(
        self,
        course_name: str,
        event_type: str,
        content: str,
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: str = "default",
    ) -> str:
        """写入一条情景记忆，返回 id。"""
        eid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        meta_str = json.dumps(metadata or {}, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO episodes
                    (id, user_id, course_name, event_type, content, importance, created_at, last_accessed_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (eid, user_id, course_name, event_type, content, importance, now, now, meta_str),
            )
        return eid

    def _touch_episode_ids(self, ids: List[str]) -> None:
        clean_ids = [str(i).strip() for i in ids if str(i).strip()]
        if not clean_ids:
            return
        now = datetime.now().isoformat()
        placeholders = ",".join(["?" for _ in clean_ids])
        with self._conn() as conn:
            conn.execute(
                f"UPDATE episodes SET last_accessed_at = ? WHERE id IN ({placeholders})",
                [now] + clean_ids,
            )

    def search_episodes(
        self,
        query: str,
        course_name: str,
        user_id: str = "default",
        event_types: Optional[List[str]] = None,
        top_k: int = 5,
        min_importance: float = 0.0,
        mode: Optional[str] = None,
        agent: Optional[str] = None,
        phase: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        基于关键词的情景记忆检索（Phase 1 简易版）。
        按 importance DESC, created_at DESC 排序后取 top_k。
        """
        backend = str(os.getenv("MEMORY_SEARCH_BACKEND", self.search_backend)).strip().lower() or "fts5"
        fetch_limit = max(top_k * 5, top_k)

        rows: List[sqlite3.Row] = []
        if backend == "like":
            rows = self._search_rows_like(
                query=query,
                course_name=course_name,
                user_id=user_id,
                event_types=event_types,
                min_importance=min_importance,
                fetch_limit=fetch_limit,
            )
        else:
            rows = self._search_rows_fts5(
                query=query,
                course_name=course_name,
                user_id=user_id,
                event_types=event_types,
                min_importance=min_importance,
                fetch_limit=fetch_limit,
            )
            if not rows:
                rows = self._search_rows_like(
                    query=query,
                    course_name=course_name,
                    user_id=user_id,
                    event_types=event_types,
                    min_importance=min_importance,
                    fetch_limit=fetch_limit,
                )

        results = []
        for row in rows:
            d = dict(row)
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                d["metadata"] = {}
            meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
            if mode and str(meta.get("mode", "")).strip() != str(mode).strip():
                continue
            if agent and str(meta.get("agent", "")).strip() != str(agent).strip():
                continue
            if phase and str(meta.get("phase", "")).strip() != str(phase).strip():
                continue
            results.append(d)
        priority = {"mistake": 0, "practice": 1, "exam": 2, "qa_summary": 3, "qa": 4}
        results.sort(
            key=lambda x: str(x.get("last_accessed_at", "") or x.get("created_at", "")),
            reverse=True,
        )
        results.sort(key=lambda x: float(x.get("importance", 0.0) or 0.0), reverse=True)
        results.sort(key=lambda x: priority.get(str(x.get("event_type", "")), 9))
        trimmed = results[:top_k]
        self._touch_episode_ids([str(item.get("id", "") or "") for item in trimmed])
        return trimmed

    def _query_terms(self, query: str) -> List[str]:
        terms = [t.strip() for t in str(query or "").split() if t.strip()]
        if not terms:
            terms = [str(query or "").strip()]
        return [t for t in terms if t]

    def _search_rows_like(
        self,
        query: str,
        course_name: str,
        user_id: str,
        event_types: Optional[List[str]],
        min_importance: float,
        fetch_limit: int,
    ) -> List[sqlite3.Row]:
        terms = self._query_terms(query)
        like_clauses = " OR ".join(["content LIKE ?" for _ in terms])
        params: List[Any] = [user_id, course_name, min_importance] + [f"%{t}%" for t in terms]

        type_clause = ""
        if event_types:
            placeholders = ",".join(["?" for _ in event_types])
            type_clause = f" AND event_type IN ({placeholders})"
            params += event_types

        sql = f"""
            SELECT * FROM episodes
            WHERE user_id = ? AND course_name = ? AND importance >= ?
              AND ({like_clauses})
              {type_clause}
            ORDER BY importance DESC, last_accessed_at DESC, created_at DESC
            LIMIT ?
        """
        params.append(fetch_limit)
        with self._conn() as conn:
            return conn.execute(sql, params).fetchall()

    def _search_rows_fts5(
        self,
        query: str,
        course_name: str,
        user_id: str,
        event_types: Optional[List[str]],
        min_importance: float,
        fetch_limit: int,
    ) -> List[sqlite3.Row]:
        terms = self._query_terms(query)
        if not terms:
            return []
        fts_query = " OR ".join(terms)
        params: List[Any] = [fts_query, user_id, course_name, min_importance]

        type_clause = ""
        if event_types:
            placeholders = ",".join(["?" for _ in event_types])
            type_clause = f" AND e.event_type IN ({placeholders})"
            params += event_types
        params.append(fetch_limit)

        sql = f"""
            SELECT e.*
            FROM episodes_fts f
            JOIN episodes e ON e.id = f.id
            WHERE f.episodes_fts MATCH ?
              AND e.user_id = ?
              AND e.course_name = ?
              AND e.importance >= ?
              {type_clause}
            ORDER BY e.importance DESC, e.last_accessed_at DESC, e.created_at DESC
            LIMIT ?
        """
        try:
            with self._conn() as conn:
                return conn.execute(sql, params).fetchall()
        except Exception:
            return []

    def get_recent_episodes(
        self,
        course_name: str,
        user_id: str = "default",
        event_types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """按时间倒序取最近若干条情景记忆。"""
        type_clause = ""
        params: List[Any] = [user_id, course_name]
        if event_types:
            placeholders = ",".join(["?" for _ in event_types])
            type_clause = f" AND event_type IN ({placeholders})"
            params += event_types
        params.append(limit)

        sql = f"""
            SELECT * FROM episodes
            WHERE user_id = ? AND course_name = ?
              {type_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            try:
                d["metadata"] = json.loads(d["metadata"])
            except Exception:
                d["metadata"] = {}
            results.append(d)
        return results

    @staticmethod
    def _qa_summary_line(content: str) -> str:
        text = str(content or "").strip()
        if not text:
            return ""
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text)
        first_line = first_line.replace("问题:", "").replace("用户要求记住:", "").strip()
        if len(first_line) > 120:
            first_line = first_line[:120].rstrip() + "..."
        return first_line

    def get_old_qa_batch_for_archive(
        self,
        course_name: str,
        user_id: str = "default",
        retain_recent: int = 50,
        batch_size: int = 20,
        max_importance: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        retain_recent = max(0, int(retain_recent or 0))
        batch_size = max(1, int(batch_size or 1))
        if max_importance is None:
            max_importance = float(os.getenv("MEMORY_QA_ARCHIVE_MAX_IMPORTANCE", "0.55"))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM episodes
                WHERE user_id = ? AND course_name = ? AND event_type = 'qa' AND importance <= ?
                ORDER BY last_accessed_at DESC, created_at DESC
                """,
                (user_id, course_name, float(max_importance)),
            ).fetchall()
        if len(rows) <= retain_recent:
            return []
        older_rows = rows[retain_recent: retain_recent + batch_size]
        results: List[Dict[str, Any]] = []
        for row in reversed(older_rows):
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata", "{}") or "{}")
            except Exception:
                item["metadata"] = {}
            if item["metadata"].get("explicit_memory_request"):
                continue
            results.append(item)
        return results

    def archive_qa_batch(
        self,
        course_name: str,
        rows: List[Dict[str, Any]],
        summary_content: str,
        metadata: Optional[Dict[str, Any]] = None,
        user_id: str = "default",
        importance: float = 0.45,
    ) -> Dict[str, Any]:
        source_ids = [str(r.get("id", "")).strip() for r in (rows or []) if str(r.get("id", "")).strip()]
        summary_text = str(summary_content or "").strip()
        if not source_ids or not summary_text:
            return {"created": False, "source_count": 0}

        meta = dict(metadata or {})
        meta.setdefault("source_ids", source_ids)
        meta.setdefault("source_count", len(source_ids))
        meta.setdefault("archived_from", "qa")
        eid = str(uuid.uuid4())
        now = datetime.now().isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO episodes
                    (id, user_id, course_name, event_type, content, importance, created_at, last_accessed_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eid,
                    user_id,
                    course_name,
                    "qa_summary",
                    summary_text,
                    float(importance),
                    now,
                    now,
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
            placeholders = ",".join(["?" for _ in source_ids])
            conn.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", source_ids)
        return {"created": True, "source_count": len(source_ids), "summary_id": eid}

    def compact_old_qa(
        self,
        course_name: str,
        user_id: str = "default",
        retain_recent: int = 80,
        batch_size: int = 20,
    ) -> Dict[str, Any]:
        """把较旧 qa 聚合为 qa_summary，减少主检索路径噪声。"""
        rows = self.get_old_qa_batch_for_archive(
            course_name=course_name,
            user_id=user_id,
            retain_recent=retain_recent,
            batch_size=batch_size,
        )
        if not rows:
            return {"created": False, "source_count": 0}
        summary_lines = []
        for row in rows:
            line = self._qa_summary_line(row.get("content", ""))
            if line:
                summary_lines.append(f"- {line}")
        if not summary_lines:
            return {"created": False, "source_count": 0}
        summary_content = "历史问答摘要：\n" + "\n".join(summary_lines[:batch_size])
        return self.archive_qa_batch(
            course_name=course_name,
            user_id=user_id,
            rows=rows,
            summary_content=summary_content,
        )

    # ── 用户画像 CRUD ─────────────────────────────────────────────────────────

    def get_profile(self, user_id: str, course_name: str) -> Dict[str, Any]:
        """获取用户画像，不存在则返回默认值。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ? AND course_name = ?",
                (user_id, course_name),
            ).fetchone()
        if row is None:
            return {
                "user_id": user_id,
                "course_name": course_name,
                "weak_points": [],
                "concept_mastery": {},
                "preference_items": [],
                "pref_style": "step_by_step",
                "total_qa": 0,
                "total_practice": 0,
                "avg_score": 0.0,
                "updated_at": None,
            }
        d = dict(row)
        try:
            d["weak_points"] = json.loads(d["weak_points"])
        except Exception:
            d["weak_points"] = []
        try:
            d["concept_mastery"] = json.loads(d.get("concept_mastery", "{}") or "{}")
            if not isinstance(d["concept_mastery"], dict):
                d["concept_mastery"] = {}
        except Exception:
            d["concept_mastery"] = {}
        try:
            d["preference_items"] = json.loads(d.get("preference_items", "[]") or "[]")
            if not isinstance(d["preference_items"], list):
                d["preference_items"] = []
        except Exception:
            d["preference_items"] = []
        return d

    def upsert_profile(self, user_id: str, course_name: str, **fields) -> None:
        """更新或插入用户画像字段（只传需要改变的字段）。"""
        profile = self.get_profile(user_id, course_name)
        profile.update(fields)
        # weak_points 序列化
        if isinstance(profile.get("weak_points"), list):
            profile["weak_points"] = json.dumps(profile["weak_points"], ensure_ascii=False)
        # concept_mastery 序列化
        if isinstance(profile.get("concept_mastery"), dict):
            profile["concept_mastery"] = json.dumps(profile["concept_mastery"], ensure_ascii=False)
        if isinstance(profile.get("preference_items"), list):
            profile["preference_items"] = json.dumps(profile["preference_items"], ensure_ascii=False)
        profile["updated_at"] = datetime.now().isoformat()

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO user_profiles
                    (user_id, course_name, weak_points, concept_mastery, preference_items, pref_style,
                     total_qa, total_practice, avg_score, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    course_name,
                    profile["weak_points"],
                    profile["concept_mastery"],
                    profile["preference_items"],
                    profile["pref_style"],
                    profile["total_qa"],
                    profile["total_practice"],
                    profile["avg_score"],
                    profile["updated_at"],
                ),
            )

    def evict_episodes_soft_cap(
        self,
        *,
        course_name: str,
        user_id: str = "default",
        soft_cap: int = 2000,
        batch_size: int = 200,
        protect_importance: float = 0.8,
    ) -> Dict[str, Any]:
        cap = max(50, int(soft_cap or 0))
        batch = max(1, int(batch_size or 1))
        protect = max(0.0, min(1.0, float(protect_importance)))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, importance, created_at, last_accessed_at, metadata
                FROM episodes
                WHERE user_id = ? AND course_name = ?
                """,
                (user_id, course_name),
            ).fetchall()
        total = len(rows)
        if total <= cap:
            return {"total": total, "removed": 0, "target_cap": cap}

        overflow = total - cap
        to_remove = min(max(overflow, 1), batch)
        candidates: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            importance = float(item.get("importance", 0.0) or 0.0)
            if importance >= protect:
                continue
            meta_raw = item.get("metadata", "{}")
            try:
                meta = json.loads(meta_raw) if isinstance(meta_raw, str) else {}
            except Exception:
                meta = {}
            if isinstance(meta, dict) and meta.get("explicit_memory_request"):
                continue
            candidates.append(item)
        candidates.sort(
            key=lambda x: (
                float(x.get("importance", 0.0) or 0.0),
                str(x.get("last_accessed_at", "") or x.get("created_at", "")),
                str(x.get("created_at", "")),
            )
        )
        remove_ids = [str(x.get("id", "")).strip() for x in candidates[:to_remove] if str(x.get("id", "")).strip()]
        if not remove_ids:
            return {"total": total, "removed": 0, "target_cap": cap}
        placeholders = ",".join(["?" for _ in remove_ids])
        with self._conn() as conn:
            conn.execute(f"DELETE FROM episodes WHERE id IN ({placeholders})", remove_ids)
        return {"total": total, "removed": len(remove_ids), "target_cap": cap}

    def get_stats(self, user_id: str = "default", course_name: str = None) -> Dict[str, Any]:
        """返回记忆库统计信息。"""
        with self._conn() as conn:
            if course_name:
                total = conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE user_id=? AND course_name=?",
                    (user_id, course_name),
                ).fetchone()[0]
                mistakes = conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE user_id=? AND course_name=? AND event_type='mistake'",
                    (user_id, course_name),
                ).fetchone()[0]
            else:
                total = conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE user_id=?", (user_id,)
                ).fetchone()[0]
                mistakes = conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE user_id=? AND event_type='mistake'",
                    (user_id,),
                ).fetchone()[0]
        return {"total_episodes": total, "mistake_episodes": mistakes}
