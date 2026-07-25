from __future__ import annotations

import json
from dataclasses import dataclass

from core.common import new_id, utc_now
from core.store import SQLiteStore

# envelope 由服务端硬校验，payload 交给 skill 自行约定（架构 §7.4）。
VISIBILITIES = ("user_visible", "model_private")
MAX_PAYLOAD_BYTES = 64 * 1024


@dataclass(frozen=True)
class Artifact:
    id: str; course_id: str; session_id: str; kind: str; visibility: str; payload: dict; created_at: str


class ArtifactStore:
    """通用跨轮产物存储：平台不为"出题中/等待作答"这类阶段定义枚举。"""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def append(self, *, course_id: str, session_id: str, kind: str, visibility: str, payload: dict) -> Artifact:
        if visibility not in VISIBILITIES:
            raise ValueError(f"visibility 只能是 {' 或 '.join(VISIBILITIES)}")
        clean_kind = (kind or "").strip()[:40]
        if not clean_kind:
            raise ValueError("artifact kind 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("artifact payload 必须是对象")
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"artifact payload 超过 {MAX_PAYLOAD_BYTES // 1024} KiB 上限")
        artifact_id, timestamp = new_id("artifact"), utc_now()
        with self._store.write() as conn:
            conn.execute(
                "INSERT INTO artifacts(id, course_id, session_id, kind, visibility, payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, course_id, session_id, clean_kind, visibility, encoded, timestamp),
            )
        return Artifact(artifact_id, course_id, session_id, clean_kind, visibility, payload, timestamp)

    def recent(self, *, session_id: str, kind: str | None = None, limit: int = 5) -> list[Artifact]:
        sql = "SELECT * FROM artifacts WHERE session_id = ?"
        params: list[object] = [session_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(max(1, min(limit, 20)))
        with self._store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._artifact(row) for row in rows]

    def latest_practice(self, *, session_id: str) -> tuple[str, int, bool, str] | None:
        """最近一次练习的事实：(practice_id, 题目数, 是否已批改, 出题时间)。"""
        recent = self.recent(session_id=session_id, kind="practice", limit=1)
        if not recent:
            return None
        latest = recent[0]
        practice_id = str(latest.payload.get("practice_id") or latest.id)
        questions = latest.payload.get("questions")
        count = len(questions) if isinstance(questions, list) else 0
        graded = any(
            str(item.payload.get("practice_id") or "") == practice_id
            for item in self.recent(session_id=session_id, kind="practice_result", limit=5)
        )
        return practice_id, count, graded, latest.created_at

    def practice_digest(self, *, session_id: str) -> str:
        """本会话最近练习的事实摘要，注入上下文供模型判断本轮该出题还是评分。

        只陈述事实，不定义"等待作答"这类阶段枚举——判断仍由 practice skill 自己做。
        """
        latest = self.latest_practice(session_id=session_id)
        if latest is None:
            return "（本会话还没有练习记录）"
        practice_id, count, graded, created_at = latest
        return f"最近一次练习：practice_id={practice_id}，{count} 道题，{'已批改' if graded else '尚未批改'}（出题时间 {created_at}）"

    def visible_for_session(self, *, session_id: str) -> list[Artifact]:
        """前端 serializer 只拿 user_visible；model_private 永远不出服务端。"""
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE session_id = ? AND visibility = 'user_visible' ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [self._artifact(row) for row in rows]

    @staticmethod
    def _artifact(row) -> Artifact:
        return Artifact(row["id"], row["course_id"], row["session_id"], row["kind"], row["visibility"], json.loads(row["payload_json"]), row["created_at"])
