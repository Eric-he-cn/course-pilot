from __future__ import annotations

import re
from pathlib import Path

from core.common import new_id, utc_now
from core.store import SQLiteStore
from contracts.knowledge import Citation, KnowledgeHit

from .concepts import concept_id_for, merge_case_variants
from .models import STAGE_INDEX_DONE, Chunk, Job, Material


_CJK = re.compile(r"[一-鿿]")
# 与 chunks_fts 的 trigram 分词器保持一致（core/store.py 迁移 19）。
_FTS_GRAM = 3

# 本教材重新索引以这次抽取结果为准（次数会真的变少）；同名概念归属别的教材时
# 不抢它的位置，次数取较大值。
_CONCEPT_MERGE = (
    " DO UPDATE SET"
    " mention_count = CASE WHEN material_id = excluded.material_id"
    " THEN excluded.mention_count ELSE MAX(mention_count, excluded.mention_count) END,"
    " page = CASE WHEN material_id = excluded.material_id THEN excluded.page ELSE page END,"
    " level = CASE WHEN material_id = excluded.material_id THEN excluded.level ELSE level END,"
    " ordinal = CASE WHEN material_id = excluded.material_id THEN excluded.ordinal ELSE ordinal END,"
    " parent_id = CASE WHEN material_id = excluded.material_id THEN excluded.parent_id ELSE parent_id END"
)
# 只差大小写的名字撞的是主键而不是 (course_id, name)，得单开一条子句接住，
# 否则整个索引作业抛 UNIQUE 冲突。合并进已有概念，显示名保持不变。
# parent_id 先一律写空，等这一批行都建完再连边，免得候选顺序决定成败。
_CONCEPT_UPSERT = (
    "INSERT INTO concepts(id, course_id, name, chapter, material_id, page, mention_count, level, ordinal, parent_id, created_at)"
    " VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?)"
    f" ON CONFLICT(course_id, name){_CONCEPT_MERGE}"
    f" ON CONFLICT(id){_CONCEPT_MERGE}"
)

_MATERIAL_SELECT = """
    SELECT m.*,
        (SELECT count(*) FROM chunks c WHERE c.material_id = m.id AND c.source_kind = 'chunk') AS chunk_count,
        (SELECT count(*) FROM chunks c WHERE c.material_id = m.id AND c.source_kind = 'chunk' AND c.embedding IS NOT NULL) AS embedded_count
    FROM materials m
"""


def _material(row: object) -> Material:
    return Material(
        id=row["id"], course_id=row["course_id"], filename=row["filename"],
        mime_type=row["mime_type"], byte_size=row["byte_size"], index_status=row["index_status"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        chunk_count=row["chunk_count"], embedded_count=row["embedded_count"],
        ocr_approved=bool(row["ocr_approved"]),
    )


def _citation(row: object, score: float) -> Citation:
    """chunks 一张表出两种来源。知识页没有文档名与页码，用概念名代替；
    chunk_id 取 wiki:<concept_id> 而不是行 id——重建一次知识页行 id 就换了，
    而去重、RRF 融合都按这个键，键跟着重建变就等于没去重。"""
    if row["source_kind"] == "wiki":
        # 正文以概念名开头（那一行是给检索用的），摘要里跳过它——抽屉标题已经写着同一个名字。
        # material_id 是这页的归属教材，检索侧按它保来源多样；界面照 kind 分流不读它。
        body = row["content"].split("\n\n", 1)[-1]
        return Citation(material_id=row["material_id"] or "", document="", page=None,
                        chunk_id=f"wiki:{row['concept_id']}",
                        snippet=body[:280], score=round(float(score), 6), kind="wiki",
                        concept_id=row["concept_id"], concept_name=row["concept_name"] or row["concept_id"])
    return Citation(material_id=row["material_id"], document=row["filename"], page=row["page"],
                    chunk_id=row["id"], snippet=row["content"][:280], score=float(score))


def _job(row: object) -> Job:
    return Job(
        id=row["id"], type=row["type"], material_id=row["material_id"], course_id=row["course_id"],
        status=row["status"], stage=row["stage"], progress=row["progress"],
        error_message=row["error_message"], retrieval_backend=row["retrieval_backend"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _purge_materials(connection, material_ids: list[str]) -> None:
    """引用教材的表全是 NO ACTION 外键，删除顺序得自己排。chunks_fts 要按 chunk_id
    反查 chunks，所以必须排在 chunks 之前。"""
    if not material_ids:
        return
    marks = ",".join("?" * len(material_ids))
    owned_concepts = f"SELECT id FROM concepts WHERE material_id IN ({marks})"
    for statement in (
        # 概念没了两张投影都留不住；原始 evidence_events 保留，日后可重算。
        f"DELETE FROM concept_mastery WHERE concept_id IN ({owned_concepts})",
        f"DELETE FROM mistake_records WHERE concept_id IN ({owned_concepts})",
        f"DELETE FROM concept_aliases WHERE concept_id IN ({owned_concepts})",
        # 重新上传同一份文件会算出同样的概念 id，标记不清掉的话投影就再也不重算了。
        f"DELETE FROM mistake_backfills WHERE course_id IN (SELECT course_id FROM materials WHERE id IN ({marks}))",
        f"DELETE FROM concepts WHERE material_id IN ({marks})",
        f"DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE material_id IN ({marks}))",
        f"DELETE FROM chunks WHERE material_id IN ({marks})",
        f"DELETE FROM jobs WHERE material_id IN ({marks})",
        f"DELETE FROM materials WHERE id IN ({marks})",
    ):
        connection.execute(statement, material_ids)


class _ConceptPlan:
    """预告与执行共用的一份账：候选先合并大小写，再按派生 id 与当前归属分堆。

    `kept` 与 `added` 会落到这份教材名下；`elsewhere` 的名字已经归同课别的教材，
    upsert 不改归属，所以它们既不是新增也不算这份教材的概念。
    """

    def __init__(self, connection, *, course_id: str, material_id: str, candidates: list[dict]) -> None:
        self.candidates = merge_case_variants(candidates)
        self.keep = [concept_id_for(course_id, candidate["name"]) for candidate in self.candidates]
        marks = ",".join("?" * len(self.keep))
        owners = {row["id"]: row["material_id"] for row in connection.execute(
            f"SELECT id, material_id FROM concepts WHERE course_id = ? AND id IN ({marks})",
            (course_id, *self.keep))}
        self.kept = [key for key in self.keep if owners.get(key) == material_id]
        self.elsewhere = [key for key in self.keep if key in owners and owners[key] != material_id]
        self.added = [key for key in self.keep if key not in owners]
        self.doomed = connection.execute(
            f"SELECT id, name FROM concepts WHERE course_id = ? AND material_id = ? AND id NOT IN ({marks})",
            (course_id, material_id, *self.keep)).fetchall()
        # 层级只会写到落在这份教材名下的行上，归别人的那些一个字都不动。
        landing = set(self.kept) | set(self.added)
        self.has_levels = any(
            candidate.get("level") is not None
            for candidate, key in zip(self.candidates, self.keep) if key in landing
        )


class KnowledgeRepository:
    """Owns the knowledge tables; other modules must use its public service/port."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def delete_material(self, material_id: str) -> Path | None:
        """删库并回传原文件路径；磁盘清理留给调用方在事务提交之后做。"""
        with self._store.write() as conn:
            row = conn.execute("SELECT storage_path FROM materials WHERE id = ?", (material_id,)).fetchone()
            if row is None:
                return None
            _purge_materials(conn, [material_id])
        return Path(row["storage_path"])

    def delete_course_materials(self, connection, *, course_id: str) -> list[Path]:
        """课程下的全部教材，与调用方共用事务。"""
        rows = connection.execute("SELECT id, storage_path FROM materials WHERE course_id = ?", (course_id,)).fetchall()
        _purge_materials(connection, [row["id"] for row in rows])
        return [Path(row["storage_path"]) for row in rows]

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

    def set_ocr_approved(self, material_id: str, approved: bool) -> None:
        with self._store.write() as conn:
            conn.execute("UPDATE materials SET ocr_approved = ?, updated_at = ? WHERE id = ?", (int(approved), utc_now(), material_id))

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

    def latest_job(self, *, material_id: str, type: str, status: str) -> Job | None:
        """这份教材最近一次到达某个状态的任务。收尾写进任务记录的东西刷新后要回读。
        同一时刻结束的两条按 id 排，而 id 是随机的——这种情况下取哪条不确定。"""
        with self._store.read() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE material_id = ? AND type = ? AND status = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT 1",
                (material_id, type, status),
            ).fetchone()
        return _job(row) if row else None

    def has_active_wiki_job(self, *, course_id: str) -> bool:
        """这门课有没有排队中或正在跑的知识页构建。构建会整页重写，编辑手写区要避开它。"""
        with self._store.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE course_id = ? AND type = 'wiki' AND status IN ('queued', 'running') LIMIT 1",
                (course_id,),
            ).fetchone()
        return row is not None

    def recover_jobs_after_restart(self) -> list[str]:
        """Fail interrupted work and return durable queued jobs for resubmission.

        停在 STAGE_INDEX_DONE 上的作业里 chunks 与向量已经写完，只差目录结构那一段。
        教材跟着降级会逼用户整份重索引（重新提取 + 重新向量化），而结构随时可以单独重算。
        """
        message = "应用重启时任务中断；请重新发起任务。"
        with self._store.write() as conn:
            interrupted = conn.execute(
                "SELECT material_id FROM jobs WHERE status = 'running' AND type = 'index' AND stage != ?",
                (STAGE_INDEX_DONE,),
            ).fetchall()
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
        """只替换教材原文那一路。知识页的行由 Wiki 构建整课替换，重建索引不该顺手抹掉它们。"""
        with self._store.write() as conn:
            old_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM chunks WHERE material_id = ? AND source_kind = 'chunk'", (material_id,))]
            if old_ids:
                conn.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", ((chunk_id,) for chunk_id in old_ids))
            conn.execute("DELETE FROM chunks WHERE material_id = ? AND source_kind = 'chunk'", (material_id,))
            for ordinal, (page, content) in enumerate(chunks):
                chunk_id = new_id("chunk")
                conn.execute(
                    "INSERT INTO chunks(id, material_id, course_id, ordinal, page, content, embedding, source_kind) VALUES (?, ?, ?, ?, ?, ?, ?, 'chunk')",
                    (chunk_id, material_id, course_id, ordinal, page, content, embeddings[ordinal] if embeddings else None),
                )
                conn.execute("INSERT INTO chunks_fts(chunk_id, course_id, content) VALUES (?, ?, ?)", (chunk_id, course_id, content))

    def replace_wiki_chunks(self, *, course_id: str, pages: list[dict], embeddings: list[bytes] | None = None) -> None:
        """整课替换知识页的检索行。按课程而不是按教材替换：一门课的几份教材共用一棵知识页，
        构建任何一份都会重写课程首页。不进 chunks_fts——一门课的页数以十计，LIKE 扫得动。
        """
        with self._store.write() as conn:
            conn.execute("DELETE FROM chunks WHERE course_id = ? AND source_kind = 'wiki'", (course_id,))
            for ordinal, page in enumerate(pages):
                conn.execute(
                    "INSERT INTO chunks(id, material_id, course_id, ordinal, page, content, embedding,"
                    " source_kind, concept_id, concept_name) VALUES (?, ?, ?, ?, NULL, ?, ?, 'wiki', ?, ?)",
                    (new_id("wiki"), page["material_id"], course_id, ordinal, page["content"],
                     embeddings[ordinal] if embeddings else None, page["concept_id"], page["concept_name"]),
                )

    def replace_wiki_chunk(self, *, course_id: str, concept_id: str, page: dict | None,
                           embedding: bytes | None = None) -> None:
        """只重写一页的检索行。page 为 None 表示这一页不该再出现在检索里，删掉即可。

        保存手写区走这里：整课替换要把每页读一遍再整批嵌入，而用户只改了其中一页。
        排序位置沿用原来那一行，新页排到末尾——ordinal 只用来给检索结果定序。
        """
        with self._store.write() as conn:
            row = conn.execute(
                "SELECT ordinal FROM chunks WHERE course_id = ? AND source_kind = 'wiki' AND concept_id = ?",
                (course_id, concept_id)).fetchone()
            conn.execute(
                "DELETE FROM chunks WHERE course_id = ? AND source_kind = 'wiki' AND concept_id = ?",
                (course_id, concept_id))
            if page is None:
                return
            if row is None:
                tail = conn.execute(
                    "SELECT max(ordinal) AS last FROM chunks WHERE course_id = ? AND source_kind = 'wiki'",
                    (course_id,)).fetchone()
                ordinal = (tail["last"] + 1) if tail and tail["last"] is not None else 0
            else:
                ordinal = row["ordinal"]
            conn.execute(
                "INSERT INTO chunks(id, material_id, course_id, ordinal, page, content, embedding,"
                " source_kind, concept_id, concept_name) VALUES (?, ?, ?, ?, NULL, ?, ?, 'wiki', ?, ?)",
                (new_id("wiki"), page["material_id"], course_id, ordinal, page["content"],
                 embedding, page["concept_id"], page["concept_name"]),
            )

    def chunk_snippets(self, *, course_id: str, ids: list[str], limit: int) -> dict[str, str]:
        """按分片 id 取本课程的正文开头。重建索引会换 id，取不到的那几个交给调用方降级处理。"""
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        with self._store.read() as conn:
            rows = conn.execute(
                f"SELECT id, content FROM chunks WHERE course_id = ? AND source_kind = 'chunk'"
                f" AND id IN ({placeholders})", (course_id, *ids),
            ).fetchall()
        return {row["id"]: row["content"][:limit] for row in rows}

    def chunks_at_pages(self, *, course_id: str, keys: list[tuple[str, str, int | None]],
                        limit: int) -> dict[tuple[str, str, int | None], tuple[str, str]]:
        """按（教材, 页码）取该位置现存的第一个分片。记录的分片 id 在重建索引后会失效，
        出处按位置重新解析才能一直点得开；按教材 id 找而不按文件名，同名教材不互串。"""
        out: dict[tuple[str, str, int | None], tuple[str, str]] = {}
        if not keys:
            return out
        with self._store.read() as conn:
            for material_id, document, page in keys:
                row = conn.execute(
                    "SELECT id, content FROM chunks WHERE course_id = ? AND source_kind = 'chunk'"
                    " AND material_id = ? AND page IS ? ORDER BY ordinal LIMIT 1",
                    (course_id, material_id, page),
                ).fetchone()
                if row is not None:
                    out[(material_id, document, page)] = (row["id"], row["content"][:limit])
        return out

    def material_page_numbers(self, *, course_id: str) -> dict[str, set[int]]:
        """每份教材在检索库里实际有正文的页码。知识页体检拿它当出处对账的基准。

        提取不出页号的教材（md、部分 docx）一页都不进这个表，调用方据此跳过对账。
        """
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT DISTINCT material_id, page FROM chunks"
                " WHERE course_id = ? AND source_kind = 'chunk' AND page IS NOT NULL", (course_id,),
            ).fetchall()
        out: dict[str, set[int]] = {}
        for row in rows:
            out.setdefault(row["material_id"], set()).add(int(row["page"]))
        return out

    def list_wiki_rows(self, *, course_id: str) -> list[dict]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT concept_id, concept_name, content FROM chunks"
                " WHERE course_id = ? AND source_kind = 'wiki' ORDER BY ordinal",
                (course_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_material_concepts(self, *, course_id: str, material_id: str, candidates: list[dict]) -> int:
        """重建本教材的概念，已存在的同名概念保持原 id 与原归属教材不动（§8.1）。

        只删这次没再抽到的概念，连它的投影一起删。留下来的保住 id，掌握度与错题历史不断档；
        整批删了再插会撞投影表的外键，也会把用户的错题记录一起抹掉。
        只差大小写的名字（Attention / attention）派生同一个 id，按同一个概念合并。
        """
        now = utc_now()
        if not candidates:
            # 抽取为空更像是抽取失败（没有标题结构的散文就抽不出东西），不是"这本教材的概念
            # 都没了"。这时候什么都不动，免得把用户的错题与掌握度一起清掉。
            with self._store.read() as conn:
                return int(conn.execute("SELECT count(*) FROM concepts WHERE course_id = ?", (course_id,)).fetchone()[0])
        with self._store.write() as conn:
            plan = _ConceptPlan(conn, course_id=course_id, material_id=material_id, candidates=candidates)
            candidates, keep = plan.candidates, plan.keep
            condition = f"course_id = ? AND material_id = ? AND id NOT IN ({','.join('?' * len(keep))})"
            doomed, params = f"SELECT id FROM concepts WHERE {condition}", (course_id, material_id, *keep)
            for statement in (
                f"DELETE FROM concept_mastery WHERE concept_id IN ({doomed})",
                f"DELETE FROM mistake_records WHERE concept_id IN ({doomed})",
                f"DELETE FROM concept_aliases WHERE concept_id IN ({doomed})",
                f"DELETE FROM concepts WHERE {condition}",
            ):
                conn.execute(statement, params)
            if plan.added:
                # 要重算投影的时刻是概念"回来"：id 由课程 + 名字派生，掉了一轮再被抽到还是
                # 同一个 id，而投影已经跟着上一次删除没了。清掉标记，下次读档案就整门课重放。
                # 概念集合没有新面孔时没有这个问题，别白重放一遍。
                conn.execute("DELETE FROM mistake_backfills WHERE course_id = ?", (course_id,))
            for candidate in candidates:
                conn.execute(
                    _CONCEPT_UPSERT,
                    (concept_id_for(course_id, candidate["name"]), course_id, candidate["name"], material_id,
                     candidate.get("page"), candidate.get("mention_count", 1), candidate.get("level"),
                     candidate.get("ordinal"), now),
                )
            # 连边单独一趟。父节点名派生的 id 与它自己的行是同一个，所以只差大小写的变体
            # 被合并掉也指得回留下来的那条；不在这一批里的父节点不连，宁可平铺也不留悬空外键。
            keep_ids = set(keep)
            for candidate in candidates:
                parent = candidate.get("parent")
                parent_id = concept_id_for(course_id, parent) if parent else None
                own_id = concept_id_for(course_id, candidate["name"])
                if parent_id is None or parent_id == own_id or parent_id not in keep_ids:
                    continue
                conn.execute(
                    "UPDATE concepts SET parent_id = ? WHERE id = ? AND material_id = ?",
                    (parent_id, own_id, material_id),
                )
            return int(conn.execute("SELECT count(*) FROM concepts WHERE course_id = ?", (course_id,)).fetchone()[0])

    def preview_material_concepts(self, *, course_id: str, material_id: str, candidates: list[dict]) -> dict:
        """算出重建目录结构会新增、删除多少概念，删掉的里面有多少挂着掌握度或错题。

        只读不写，判据与 replace_material_concepts 用同一份账（`_ConceptPlan`），
        所以报出的数字就是真正会发生的事。
        """
        if not candidates:
            # 抽取为空时重建是空操作（见 replace_material_concepts），预告也要照这个口径。
            return {"empty": True, "candidates": 0, "added": 0, "removed": 0, "kept": 0,
                    "owned_elsewhere": 0, "has_levels": False,
                    "at_risk": 0, "removed_names": [], "at_risk_names": []}
        with self._store.read() as conn:
            plan = _ConceptPlan(conn, course_id=course_id, material_id=material_id, candidates=candidates)
            risky: set[str] = set()
            if plan.doomed:
                ids = [row["id"] for row in plan.doomed]
                spots = ",".join("?" * len(ids))
                for table in ("concept_mastery", "mistake_records"):
                    risky |= {row["concept_id"] for row in
                              conn.execute(f"SELECT concept_id FROM {table} WHERE concept_id IN ({spots})", ids)}
        names = {row["id"]: row["name"] for row in plan.doomed}
        return {
            "empty": False, "candidates": len(plan.candidates),
            "added": len(plan.added), "removed": len(plan.doomed), "kept": len(plan.kept),
            "owned_elsewhere": len(plan.elsewhere), "has_levels": plan.has_levels,
            "removed_names": sorted(names.values()),
            "at_risk": len(risky), "at_risk_names": sorted(names[concept_id] for concept_id in risky),
        }

    def material_concept_stats(self, *, course_id: str) -> list[dict]:
        """每份教材抽到多少概念、其中多少条带层级。结构状态由这两个数推导，不另存状态列。"""
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT m.id AS material_id, m.filename, m.index_status,"
                " (SELECT count(*) FROM concepts c WHERE c.material_id = m.id) AS concepts,"
                " (SELECT count(*) FROM concepts c WHERE c.material_id = m.id AND c.level IS NOT NULL) AS leveled"
                " FROM materials m WHERE m.course_id = ? ORDER BY m.created_at DESC",
                (course_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_concepts(self, *, course_id: str, limit: int = 60) -> list[dict]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT id, name, page, mention_count FROM concepts WHERE course_id = ? ORDER BY mention_count DESC, page, name LIMIT ?",
                (course_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_concept_tree(self, *, course_id: str) -> list[dict]:
        """整份概念目录，按教材里的先后返回。ordinal 来自目录顺序；没有书签的教材没有它，
        退回插入顺序。不能只靠 rowid：upsert 保留旧行，改版重索引后顺序会停在上一版。"""
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT id, name, page, level, parent_id, mention_count, material_id"
                " FROM concepts WHERE course_id = ?"
                " ORDER BY material_id, ordinal IS NULL, ordinal, rowid",
                (course_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_material_concept_tree(self, *, material_id: str) -> list[dict]:
        """一份教材的概念目录，按教材里的先后返回。Wiki 自底向上遍历要的就是这个顺序。"""
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT id, name, page, level, parent_id FROM concepts WHERE material_id = ?"
                " ORDER BY ordinal IS NULL, ordinal, rowid",
                (material_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_material_chunks(self, *, material_id: str) -> list[dict]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT id, ordinal, page, content FROM chunks WHERE material_id = ?"
                " AND source_kind = 'chunk' ORDER BY ordinal",
                (material_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def material_ids(self, *, course_id: str) -> set[str]:
        with self._store.read() as conn:
            return {row["id"] for row in conn.execute("SELECT id FROM materials WHERE course_id = ?", (course_id,))}

    def concept_ids(self, *, course_id: str) -> set[str]:
        with self._store.read() as conn:
            return {row["id"] for row in conn.execute("SELECT id FROM concepts WHERE course_id = ?", (course_id,))}

    def concept_exists(self, *, course_id: str, concept_id: str) -> bool:
        with self._store.read() as conn:
            return conn.execute("SELECT 1 FROM concepts WHERE id = ? AND course_id = ?", (concept_id, course_id)).fetchone() is not None

    def load_course_embeddings(self, *, course_id: str, source_kind: str = "chunk") -> list[tuple[str, bytes]]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT id, embedding FROM chunks WHERE course_id = ? AND source_kind = ? AND embedding IS NOT NULL ORDER BY rowid",
                (course_id, source_kind),
            ).fetchall()
        return [(row["id"], row["embedding"]) for row in rows]

    def wiki_embeddings(self, *, course_id: str) -> list[tuple[str, str, bytes]]:
        """知识页的向量，按（概念, 教材）取。行 id 每次构建都会换，跨页配对只能按 concept_id 对齐。"""
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT concept_id, material_id, embedding FROM chunks WHERE course_id = ?"
                " AND source_kind = 'wiki' AND embedding IS NOT NULL ORDER BY ordinal",
                (course_id,),
            ).fetchall()
        return [(row["concept_id"] or "", row["material_id"] or "", row["embedding"]) for row in rows]

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
                hits.append(KnowledgeHit(citation=_citation(row, score), content=row["content"]))
        return hits

    def search_wiki(self, *, course_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        """知识页的词面检索。页数以十计，直接 LIKE 扫全课再按词面重合度排，
        不建 FTS：trigram 索引是为几千段教材原文准备的，这里用不上。"""
        tokens = [token for token in re.findall(r"[^\W_一-鿿]+|[一-鿿]+", query, flags=re.UNICODE) if token]
        if not tokens:
            return []
        terms = self._fallback_terms(tokens)
        clauses = " OR ".join("lower(content) LIKE lower(?)" for _ in terms)
        with self._store.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM chunks WHERE course_id = ? AND source_kind = 'wiki' AND ({clauses})",
                (course_id, *(f"%{term}%" for term in terms)),
            ).fetchall()
        ranked = sorted(rows, key=lambda row: self._term_overlap_score(row["content"], terms), reverse=True)[:limit]
        return [KnowledgeHit(citation=_citation(row, self._term_overlap_score(row["content"], terms)), content=row["content"])
                for row in ranked]

    def wiki_hits_by_ids(self, *, scored: list[tuple[str, float]]) -> list[KnowledgeHit]:
        if not scored:
            return []
        placeholders = ",".join("?" * len(scored))
        with self._store.read() as conn:
            rows = {row["id"]: row for row in conn.execute(
                f"SELECT * FROM chunks WHERE id IN ({placeholders})", [row_id for row_id, _ in scored])}
        return [KnowledgeHit(citation=_citation(rows[row_id], score), content=rows[row_id]["content"])
                for row_id, score in scored if row_id in rows]

    def search(self, *, course_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        # 中英混排必须在文种边界切开（"你有没有Deep"≠一个词），否则英文词
        # 永远无法命中英文教材。
        tokens = [token for token in re.findall(r"[^\W_一-鿿]+|[一-鿿]+", query, flags=re.UNICODE) if token]
        if not tokens:
            return []
        # Quote tokens to avoid FTS syntax injection.  FTS is an optimization; LIKE remains the deterministic fallback.
        # OR + bm25：混合语言查询里注定缺席的词（如中文串之于英文书）不应否决整次检索。
        fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in self._fts_terms(tokens))
        with self._store.read() as conn:
            rows = []
            try:
                if fts_query:  # 全是短词时这一路没得查，直接落兜底
                    rows = conn.execute(
                        "SELECT c.*, m.filename, bm25(chunks_fts) AS rank FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.chunk_id JOIN materials m ON m.id = c.material_id WHERE chunks_fts.course_id = ? AND c.source_kind = 'chunk' AND chunks_fts MATCH ? ORDER BY rank LIMIT ?",
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
                    f"SELECT c.*, m.filename FROM chunks c JOIN materials m ON m.id = c.material_id WHERE c.course_id = ? AND c.source_kind = 'chunk' AND ({clauses}) ORDER BY c.ordinal LIMIT ?",
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
        return [KnowledgeHit(citation=_citation(row, -row["rank"]), content=row["content"]) for row in rows]

    @staticmethod
    def _fts_terms(tokens: list[str]) -> list[str]:
        """trigram 索引只存三字滑窗，查询侧也要同样切开：整串「链式法则怎么用」
        当短语匹配，命中不了只写着「链式法则」的段落。
        不足三字的词（中文的「极限」、英文的 AI/ML）在 trigram 索引里注定 0 命中，
        直接剔掉，交给 LIKE 兜底，别让它们白占一个 OR 分支。"""
        terms: list[str] = []
        for token in tokens:
            if len(token) < _FTS_GRAM:
                continue
            if _CJK.match(token):
                terms.extend(token[index:index + _FTS_GRAM] for index in range(len(token) - _FTS_GRAM + 1))
            else:
                terms.append(token)
        return list(dict.fromkeys(terms))

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
