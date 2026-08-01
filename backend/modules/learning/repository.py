from __future__ import annotations
import json
from core.store import SQLiteStore
from .mastery import OBJECTIVE_KINDS
class LearningRepository:
    def __init__(self, store: SQLiteStore) -> None: self._store = store
    def count_events(self, *, course_id: str) -> int:
        with self._store.read() as c: return int(c.execute("SELECT count(*) FROM evidence_events WHERE course_id = ?", (course_id,)).fetchone()[0])
    def list_recent_event_rows(self, *, course_id: str, limit: int):
        with self._store.read() as c:
            return c.execute(
                "SELECT e.*, c.name AS concept_name FROM evidence_events e LEFT JOIN concepts c ON c.id = e.concept_id"
                " WHERE e.course_id = ? ORDER BY e.created_at DESC LIMIT ?",
                (course_id, limit),
            ).fetchall()
    def concept_row(self, concept_id: str):
        with self._store.read() as c: return c.execute("SELECT id, course_id, name FROM concepts WHERE id = ?", (concept_id,)).fetchone()
    def insert_event(self, *, event_id: str, course_id: str, concept_id: str | None, attribution_status: str, topic_hint: str | None, kind: str, payload: dict, timestamp: str) -> None:
        with self._store.write() as c:
            c.execute(
                "INSERT INTO evidence_events(id, course_id, concept_id, attribution_status, topic_hint, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, course_id, concept_id, attribution_status, topic_hint, kind, json.dumps(payload, ensure_ascii=False), timestamp),
            )
    def concept_event_rows(self, concept_id: str) -> list[dict]:
        """该概念的完整事件流，按时间正序——掌握度投影由此重放得出。"""
        with self._store.read() as c:
            rows = c.execute("SELECT kind, payload_json, created_at FROM evidence_events WHERE concept_id = ? ORDER BY created_at ASC, rowid ASC", (concept_id,)).fetchall()
        return [{"kind": row["kind"], "payload": json.loads(row["payload_json"] or "{}"), "created_at": row["created_at"]} for row in rows]
    def upsert_mastery(self, *, concept_id: str, course_id: str, bkt_p: float, stability: float, difficulty: float, objective_events: int, last_reviewed_at: str | None, due_at: str | None, algorithm_version: str, timestamp: str) -> None:
        with self._store.write() as c:
            c.execute(
                "INSERT INTO concept_mastery(concept_id, course_id, bkt_p, fsrs_stability, fsrs_difficulty, objective_events, last_reviewed_at, due_at, algorithm_version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(concept_id) DO UPDATE SET bkt_p=excluded.bkt_p, fsrs_stability=excluded.fsrs_stability, fsrs_difficulty=excluded.fsrs_difficulty,"
                " objective_events=excluded.objective_events, last_reviewed_at=excluded.last_reviewed_at, due_at=excluded.due_at,"
                " algorithm_version=excluded.algorithm_version, updated_at=excluded.updated_at",
                (concept_id, course_id, bkt_p, stability, difficulty, objective_events, last_reviewed_at, due_at, algorithm_version, timestamp),
            )
    def mastery_rows(self, *, course_id: str, limit: int = 60) -> list[dict]:
        with self._store.read() as c:
            rows = c.execute(
                "SELECT m.*, c.name FROM concept_mastery m JOIN concepts c ON c.id = m.concept_id WHERE m.course_id = ?"
                " ORDER BY m.bkt_p ASC, c.name LIMIT ?",
                (course_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
    def projectable_concept_ids(self, *, course_id: str) -> list[str]:
        """该有投影行的概念：有客观作答证据，且概念还在目录里。

        JOIN 掉已删概念——投影表有外键，写进去会撞。只认客观证据，免得给「只有追问记录」
        的概念凭空造出一行零证据的掌握度。
        """
        marks = ",".join("?" * len(OBJECTIVE_KINDS))
        with self._store.read() as c:
            rows = c.execute(
                "SELECT DISTINCT e.concept_id FROM evidence_events e JOIN concepts c ON c.id = e.concept_id"
                f" WHERE e.course_id = ? AND e.kind IN ({marks})",
                (course_id, *sorted(OBJECTIVE_KINDS)),
            ).fetchall()
        return [row[0] for row in rows]
    def upsert_mistake(self, *, record_id: str, concept_id: str, course_id: str, status: str, wrong_count: int, streak: int, first_wrong_at: str, last_wrong_at: str, graduated_at: str | None, relapse_count: int) -> None:
        with self._store.write() as c:
            c.execute(
                "INSERT INTO mistake_records(id, course_id, concept_id, status, wrong_count, streak, first_wrong_at, last_wrong_at, graduated_at, relapse_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(course_id, concept_id) DO UPDATE SET status=excluded.status, wrong_count=excluded.wrong_count,"
                " streak=excluded.streak, first_wrong_at=excluded.first_wrong_at, last_wrong_at=excluded.last_wrong_at,"
                " graduated_at=excluded.graduated_at, relapse_count=excluded.relapse_count",
                (record_id, course_id, concept_id, status, wrong_count, streak, first_wrong_at, last_wrong_at, graduated_at, relapse_count),
            )
    def mistake_backfill_done(self, *, course_id: str) -> bool:
        with self._store.read() as c:
            return c.execute("SELECT 1 FROM mistake_backfills WHERE course_id = ?", (course_id,)).fetchone() is not None
    def mark_mistake_backfill_done(self, *, course_id: str, timestamp: str) -> None:
        with self._store.write() as c:
            c.execute("INSERT OR REPLACE INTO mistake_backfills(course_id, completed_at) VALUES (?, ?)", (course_id, timestamp))
    def count_mistakes(self, *, course_id: str, status: str | None = None) -> int:
        sql = "SELECT count(*) FROM mistake_records WHERE course_id = ?"
        params: tuple = (course_id,)
        if status:
            sql, params = sql + " AND status = ?", (course_id, status)
        with self._store.read() as c: return int(c.execute(sql, params).fetchone()[0])
    def mistake_rows(self, *, course_id: str, limit: int = 20) -> list[dict]:
        """活跃错题排在前面，同组内最近错的排前。

        limit 作用于合并后的列表，所以活跃条目够多时会一条已毕业的都排不上。
        这是一页而不是全量，总数看 count_mistakes。
        """
        with self._store.read() as c:
            rows = c.execute(
                "SELECT m.*, c.name FROM mistake_records m JOIN concepts c ON c.id = m.concept_id WHERE m.course_id = ?"
                " ORDER BY CASE m.status WHEN 'active' THEN 0 ELSE 1 END, m.last_wrong_at DESC LIMIT ?",
                (course_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
    def unattributed_rows(self, *, course_id: str, limit: int = 20) -> list[dict]:
        """未归因主题按 topic_hint 聚合频次，供管理页补录（架构 §11）。"""
        with self._store.read() as c:
            rows = c.execute(
                "SELECT topic_hint, count(*) AS hits, max(created_at) AS last_seen FROM evidence_events"
                " WHERE course_id = ? AND attribution_status = 'unattributed' AND topic_hint IS NOT NULL"
                " GROUP BY topic_hint ORDER BY hits DESC, last_seen DESC LIMIT ?",
                (course_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
