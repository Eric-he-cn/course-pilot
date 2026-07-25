from __future__ import annotations

import re
from pathlib import Path

from core.common import new_id, utc_now
from core.store import SQLiteStore
from contracts.knowledge import Citation, KnowledgeHit

from .concepts import concept_id_for
from .models import Chunk, Job, Material


_MATERIAL_SELECT = """
    SELECT m.*,
        (SELECT count(*) FROM chunks c WHERE c.material_id = m.id) AS chunk_count,
        (SELECT count(*) FROM chunks c WHERE c.material_id = m.id AND c.embedding IS NOT NULL) AS embedded_count
    FROM materials m
"""


def _material(row: object) -> Material:
    return Material(
        id=row["id"], course_id=row["course_id"], filename=row["filename"],
        mime_type=row["mime_type"], byte_size=row["byte_size"], index_status=row["index_status"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        chunk_count=row["chunk_count"], embedded_count=row["embedded_count"],
    )


def _job(row: object) -> Job:
    return Job(
        id=row["id"], type=row["type"], material_id=row["material_id"], course_id=row["course_id"],
        status=row["status"], stage=row["stage"], progress=row["progress"],
        error_message=row["error_message"], retrieval_backend=row["retrieval_backend"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class KnowledgeRepository:
    """Owns the knowledge tables; other modules must use its public service/port."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def health_check(self) -> int:
        """Return the applied migration version without exposing the shared store."""
        with self._store.read() as conn:
            conn.execute("SELECT 1").fetchone()
            conn.execute("SELECT count(*) FROM chunks_fts").fetchone()
            return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])

    def create_material(self, *, course_id: str, filename: str, storage_path: Path, mime_type: str, byte_size: int) -> Material:
        material_id, now = new_id("material"), utc_now()
        with self._store.write() as conn:
            conn.execute(
                "INSERT INTO materials(id, course_id, filename, storage_path, mime_type, byte_size, index_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'uploaded', ?, ?)",
                (material_id, course_id, filename, str(storage_path), mime_type, byte_size, now, now),
            )
        return self.get_material(material_id)  # type: ignore[return-value]

    def get_material(self, material_id: str) -> Material | None:
        with self._store.read() as conn:
            row = conn.execute(f"{_MATERIAL_SELECT} WHERE m.id = ?", (material_id,)).fetchone()
        return _material(row) if row else None

    def material_storage_path(self, material_id: str) -> Path | None:
        with self._store.read() as conn:
            row = conn.execute("SELECT storage_path FROM materials WHERE id = ?", (material_id,)).fetchone()
        return Path(row["storage_path"]) if row else None

    def list_materials(self, *, course_id: str) -> list[Material]:
        with self._store.read() as conn:
            rows = conn.execute(f"{_MATERIAL_SELECT} WHERE m.course_id = ? ORDER BY m.created_at DESC", (course_id,)).fetchall()
        return [_material(row) for row in rows]

    def set_material_status(self, material_id: str, status: str) -> None:
        with self._store.write() as conn:
            conn.execute("UPDATE materials SET index_status = ?, updated_at = ? WHERE id = ?", (status, utc_now(), material_id))

    def create_job(self, *, type: str, material_id: str, course_id: str, retrieval_backend: str | None = None) -> Job:
        job_id, now = new_id("job"), utc_now()
        with self._store.write() as conn:
            conn.execute(
                "INSERT INTO jobs(id, type, material_id, course_id, status, stage, progress, error_message, retrieval_backend, created_at, updated_at) VALUES (?, ?, ?, ?, 'queued', 'queued', 0, NULL, ?, ?, ?)",
                (job_id, type, material_id, course_id, retrieval_backend, now, now),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_job(self, job_id: str) -> Job | None:
        with self._store.read() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def recover_jobs_after_restart(self) -> list[str]:
        """Fail interrupted work and return durable queued jobs for resubmission."""
        message = "应用重启时任务中断；请重新发起任务。"
        with self._store.write() as conn:
            interrupted = conn.execute("SELECT material_id FROM jobs WHERE status = 'running' AND type = 'index'").fetchall()
            conn.execute(
                "UPDATE jobs SET status='failed', stage='failed', error_message=?, updated_at=? WHERE status='running'",
                (message, utc_now()),
            )
            if interrupted:
                conn.executemany(
                    "UPDATE materials SET index_status='failed', updated_at=? WHERE id=?",
                    ((utc_now(), row["material_id"]) for row in interrupted),
                )
            return [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at ASC")]

    def claim_queued_job(self, job_id: str) -> Job | None:
        """Atomically move a queued job to running so duplicate submissions are safe."""
        with self._store.write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='running', stage='starting', progress=1, updated_at=? WHERE id=? AND status='queued'",
                (utc_now(), job_id),
            )
        return self.get_job(job_id) if cursor.rowcount else None

    def update_job(self, job_id: str, *, status: str, stage: str, progress: int, error_message: str | None = None, retrieval_backend: str | None = None) -> Job:
        with self._store.write() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, stage=?, progress=?, error_message=?, retrieval_backend=COALESCE(?, retrieval_backend), updated_at=? WHERE id=?",
                (status, stage, max(0, min(100, progress)), error_message, retrieval_backend, utc_now(), job_id),
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def replace_chunks(self, *, material_id: str, course_id: str, chunks: list[tuple[int | None, str]], embeddings: list[bytes] | None = None) -> None:
        with self._store.write() as conn:
            old_ids = [row["id"] for row in conn.execute("SELECT id FROM chunks WHERE material_id = ?", (material_id,))]
            if old_ids:
                conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", ((chunk_id,) for chunk_id in old_ids))
            conn.execute("DELETE FROM chunks WHERE material_id = ?", (material_id,))
            for ordinal, (page, content) in enumerate(chunks):
                chunk_id = new_id("chunk")
                conn.execute(
                    "INSERT INTO chunks(id, material_id, course_id, ordinal, page, content, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (chunk_id, material_id, course_id, ordinal, page, content, embeddings[ordinal] if embeddings else None),
                )
                conn.execute("INSERT INTO chunks_fts(chunk_id, course_id, content) VALUES (?, ?, ?)", (chunk_id, course_id, content))

    def replace_material_concepts(self, *, course_id: str, material_id: str, candidates: list[dict]) -> int:
        """重建本教材的概念，已存在的同名概念保持原 id 与原归属教材不动（§8.1）。"""
        now = utc_now()
        with self._store.write() as conn:
            conn.execute("DELETE FROM concepts WHERE course_id = ? AND material_id = ?", (course_id, material_id))
            for candidate in candidates:
                conn.execute(
                    "INSERT INTO concepts(id, course_id, name, chapter, material_id, page, mention_count, created_at)"
                    " VALUES (?, ?, ?, NULL, ?, ?, ?, ?)"
                    " ON CONFLICT(course_id, name) DO UPDATE SET mention_count = MAX(mention_count, excluded.mention_count)",
                    (concept_id_for(course_id, candidate["name"]), course_id, candidate["name"], material_id,
                     candidate.get("page"), candidate.get("mention_count", 1), now),
                )
            return int(conn.execute("SELECT count(*) FROM concepts WHERE course_id = ?", (course_id,)).fetchone()[0])

    def list_concepts(self, *, course_id: str, limit: int = 60) -> list[dict]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT id, name, page, mention_count FROM concepts WHERE course_id = ? ORDER BY mention_count DESC, name LIMIT ?",
                (course_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_course_embeddings(self, *, course_id: str) -> list[tuple[str, bytes]]:
        with self._store.read() as conn:
            rows = conn.execute("SELECT id, embedding FROM chunks WHERE course_id = ? AND embedding IS NOT NULL ORDER BY rowid", (course_id,)).fetchall()
        return [(row["id"], row["embedding"]) for row in rows]

    def hits_by_chunk_ids(self, *, scored: list[tuple[str, float]]) -> list[KnowledgeHit]:
        if not scored:
            return []
        ids = [chunk_id for chunk_id, _ in scored]
        placeholders = ",".join("?" * len(ids))
        with self._store.read() as conn:
            rows = {
                row["id"]: row
                for row in conn.execute(
                    f"SELECT c.*, m.filename FROM chunks c JOIN materials m ON m.id = c.material_id WHERE c.id IN ({placeholders})", ids
                )
            }
        hits = []
        for chunk_id, score in scored:
            row = rows.get(chunk_id)
            if row is not None:
                hits.append(
                    KnowledgeHit(
                        citation=Citation(material_id=row["material_id"], document=row["filename"], page=row["page"], chunk_id=row["id"], snippet=row["content"][:280], score=score),
                        content=row["content"],
                    )
                )
        return hits

    def search(self, *, course_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        # 中英混排必须在文种边界切开（"你有没有Deep"≠一个词），否则英文词
        # 永远无法命中英文教材。
        tokens = [token for token in re.findall(r"[^\W_一-鿿]+|[一-鿿]+", query, flags=re.UNICODE) if token]
        if not tokens:
            return []
        # Quote tokens to avoid FTS syntax injection.  FTS is an optimization; LIKE remains the deterministic fallback.
        # OR + bm25：混合语言查询里注定缺席的词（如中文串之于英文书）不应否决整次检索。
        fts_query = " OR ".join(f'"{token.replace(chr(34), "")}"' for token in tokens)
        with self._store.read() as conn:
            try:
                rows = conn.execute(
                    "SELECT c.*, m.filename, bm25(chunks_fts) AS rank FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id JOIN materials m ON m.id = c.material_id WHERE chunks_fts.course_id = ? AND chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                    (course_id, fts_query, limit),
                ).fetchall()
            except Exception:
                rows = []
            if not rows:
                # unicode61 regards a full Chinese run as one token.  A natural
                # question such as "高等数学 II 的链式法则怎么用？" therefore
                # cannot be matched as one FTS phrase.  Search its CJK n-grams
                # in the same *course* and rank by overlapping terms instead.
                terms = self._fallback_terms(tokens)
                clauses = " OR ".join("lower(c.content) LIKE lower(?)" for _ in terms)
                rows = conn.execute(
                    f"SELECT c.*, m.filename FROM chunks c JOIN materials m ON m.id = c.material_id WHERE c.course_id = ? AND ({clauses}) ORDER BY c.ordinal LIMIT ?",
                    (course_id, *(f"%{term}%" for term in terms), min(200, limit * 12)),
                ).fetchall()
                rows = sorted(
                    rows,
                    key=lambda row: self._term_overlap_score(row["content"], terms),
                    reverse=True,
                )[:limit]
                # Keep a single rank shape for the serializer below. SQLite
                # rows are immutable, so convert only fallback result rows.
                rows = [dict(row, rank=-self._term_overlap_score(row["content"], terms)) for row in rows]
        return [
            KnowledgeHit(
                citation=Citation(material_id=row["material_id"], document=row["filename"], page=row["page"], chunk_id=row["id"], snippet=row["content"][:280], score=float(-row["rank"])),
                content=row["content"],
            )
            for row in rows
        ]

    @staticmethod
    def _fallback_terms(tokens: list[str]) -> list[str]:
        terms: list[str] = []
        for token in tokens:
            clean = token.strip()
            if clean:
                terms.append(clean)
            for run in re.findall(r"[\u4e00-\u9fff]{2,}", clean):
                # Preserve the phrase but add bigrams/trigrams for the SQLite
                # fallback only; this is intentionally not a cross-course scan.
                terms.extend(run[index:index + 2] for index in range(len(run) - 1))
                terms.extend(run[index:index + 3] for index in range(len(run) - 2))
        return list(dict.fromkeys(terms))

    @staticmethod
    def _term_overlap_score(content: str, terms: list[str]) -> float:
        lowered = content.lower()
        return float(sum(len(term) * lowered.count(term.lower()) for term in terms))
