from __future__ import annotations

import re
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path
from typing import Callable

from core.common import new_id
from core.settings import Settings
from contracts.embedding import EmbedderPort
from contracts.knowledge import ConceptRef, KnowledgeHit, ResolvedKnowledgeScope
from contracts.reranker import RerankerPort

from .api import KnowledgeFeatureDisabledError, MaterialNotIndexedError
from .concepts import extract_candidates
from . import scanned
from .extract import SUPPORTED_SUFFIXES, extract_pages
from .models import Job, Material
from .repository import KnowledgeRepository


class KnowledgeService:
    """Local-only RAG fallback and optional Wiki job skeleton.

    The service implements ``KnowledgeSearchPort`` by exposing ``search``.  Agent
    code receives the port through bootstrap and never receives this repository.
    """

    _ALLOWED_SUFFIXES = SUPPORTED_SUFFIXES

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        settings: Settings,
        wiki_is_enabled: Callable[[str], bool] | None = None,
        embedder: EmbedderPort | None = None,
        reranker: RerankerPort | None = None,
        transcriber: object | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._wiki_is_enabled = wiki_is_enabled or (lambda _course_id: False)
        self._embedder = embedder
        self._reranker = reranker
        self._transcriber = transcriber

    def upload_material(self, *, course_id: str, filename: str, mime_type: str, content: bytes) -> Material:
        # 文件名会进提示词与界面：压掉空白、限长，避免换行伪造出新的提示词规则。
        safe_name = re.sub(r"\s+", " ", Path(filename).name).strip()[:120]
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in self._ALLOWED_SUFFIXES:
            raise ValueError("仅支持 PDF、Word、PowerPoint、TXT 或 MD 教材")
        if not content:
            raise ValueError("教材不能为空")
        if len(content) > self._settings.material_max_bytes:
            limit_mib = self._settings.material_max_bytes / (1024 * 1024)
            raise ValueError(f"教材超过 {limit_mib:g} MiB 上限")
        if suffix == ".pdf" and not content.startswith(b"%PDF"):
            raise ValueError("PDF 文件头无效")
        if suffix in {".txt", ".md"} and not mime_type.startswith("text/"):
            mime_type = "text/markdown" if suffix == ".md" else "text/plain"
        self._settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        # The display name never participates in a filesystem path.  A generated
        # name prevents same-named uploads from overwriting one another.
        storage_path = self._settings.uploads_dir / f"{new_id('upload')}{suffix}"
        storage_path.write_bytes(content)
        return self._repository.create_material(
            course_id=course_id, filename=safe_name, storage_path=storage_path, mime_type=mime_type, byte_size=len(content),
        )

    def list_materials(self, *, course_id: str) -> list[Material]:
        return self._repository.list_materials(course_id=course_id)

    def enqueue_index(self, *, material_id: str) -> Job:
        material = self._material_or_error(material_id)
        self._repository.set_material_status(material.id, "queued")
        return self._repository.create_job(type="index", material_id=material.id, course_id=material.course_id, retrieval_backend="sqlite_fts")

    def estimate_ocr(self, *, material_id: str) -> dict[str, object]:
        """真 OCR 前两页量出成本，再按页数外推。花钱的动作要先让用户看见账单。"""
        path = self._pdf_or_error(material_id)
        if self._transcriber is None:
            raise KnowledgeFeatureDisabledError("没有配置 vision 槽位，无法 OCR（见 .env 的 VISION_*）")

        def sample(image: bytes) -> dict[str, int]:
            result = self._transcriber.transcribe(content=image, mime_type="image/jpeg")
            return {key: int(value) for key, value in (result.usage or {}).items() if isinstance(value, int)}

        return scanned.estimate(path, sample).as_dict()

    def approve_ocr(self, *, material_id: str) -> Job:
        """用户确认账单后才允许走 OCR。这个批准会留在库里，重新索引不必再问一次。"""
        self._pdf_or_error(material_id)
        if self._transcriber is None:
            raise KnowledgeFeatureDisabledError("没有配置 vision 槽位，无法 OCR（见 .env 的 VISION_*）")
        self._repository.set_ocr_approved(material_id, True)
        return self.enqueue_index(material_id=material_id)

    def _pdf_or_error(self, material_id: str):
        material = self._material_or_error(material_id)
        if Path(material.filename).suffix.lower() != ".pdf":
            raise ValueError("只有 PDF 需要 OCR")
        path = self._repository.material_storage_path(material.id)
        if path is None or not path.is_file():
            raise ValueError("教材文件不存在")
        return path

    def get_job(self, *, job_id: str) -> Job | None:
        return self._repository.get_job(job_id)

    def enqueue_wiki_build(self, *, material_id: str) -> Job:
        material = self._material_or_error(material_id)
        if not self._wiki_is_enabled(material.course_id):
            raise KnowledgeFeatureDisabledError("该课程尚未启用 Wiki")
        if material.index_status != "indexed":
            raise MaterialNotIndexedError("教材尚未完成索引")
        return self._repository.create_job(type="wiki", material_id=material.id, course_id=material.course_id)

    def run_job(self, *, job_id: str) -> Job | None:
        """Execute a previously persisted job on the local worker, never in HTTP."""
        job = self._repository.claim_queued_job(job_id)
        if job is None:
            return self.get_job(job_id=job_id)
        material = self._material_or_error(job.material_id)
        if job.type == "index":
            return self._run_index(job, material)
        if job.type == "wiki":
            return self._run_wiki(job, material)
        return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message="未知任务类型")

    def recover_jobs_after_restart(self) -> list[str]:
        return self._repository.recover_jobs_after_restart()

    def reject_queued_job(self, *, job_id: str, reason: str) -> Job | None:
        job = self.get_job(job_id=job_id)
        if job is None or job.status != "queued":
            return job
        return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=reason)

    def _run_wiki(self, job: Job, material: Material) -> Job:
        try:
            self._repository.update_job(job.id, status="running", stage="reading_index", progress=20)
            output = self._settings.data_dir / "wiki" / material.course_id
            output.mkdir(parents=True, exist_ok=True)
            source = self.search_course(course_id=material.course_id, query=Path(material.filename).stem, limit=6)
            outline = "\n\n".join(hit.content[:500] for hit in source) or "（教材已索引；等待后续 Wiki 解析。）"
            (output / f"{material.id}.md").write_text(f"# {material.filename}\n\n{outline}\n", encoding="utf-8")
            return self._repository.update_job(job.id, status="completed", stage="wiki_completed", progress=100)
        except Exception as error:
            return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=str(error))

    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]:
        """Agent-only search: the course is a server-issued resolver result."""
        return self.search_course(course_id=scope.course_id, query=query, limit=limit)

    def material_names(self, *, scope: ResolvedKnowledgeScope) -> list[str]:
        return [material.filename for material in self.list_materials(course_id=scope.course_id)]

    def concepts(self, *, scope: ResolvedKnowledgeScope, limit: int = 60) -> list[ConceptRef]:
        return self.list_course_concepts(course_id=scope.course_id, limit=limit)

    def concept_exists(self, course_id: str, concept_id: str) -> bool:
        """按 id 精确判断，不受概念清单的展示上限影响。"""
        return self._repository.concept_exists(course_id=course_id, concept_id=concept_id)

    def list_course_concepts(self, *, course_id: str, limit: int = 60) -> list[ConceptRef]:
        return [ConceptRef(row["id"], row["name"], row["page"]) for row in self._repository.list_concepts(course_id=course_id, limit=limit)]

    def search_course(self, *, course_id: str, query: str, limit: int = 6) -> list[KnowledgeHit]:
        """词面 + 语义召回，cross-encoder 精排，低于阈值的丢掉。

        召回比最终条数宽一些（rag_rerank_candidates），精排负责把真正相关的挑出来。
        """
        if not query.strip():
            return []
        limit = max(1, min(limit, 20))
        pool = max(limit, self._settings.rag_rerank_candidates)
        lexical = self._repository.search(course_id=course_id, query=query, limit=pool)
        dense = self._dense_search(course_id=course_id, query=query, limit=pool)
        if not dense:
            # 语义检索不可用（模型未配置、加载失败、向量维度不一致）时，词面结果照原样返回。
            return lexical[:limit]
        # 余弦下限只作用在召回阶段，默认关闭：它的绝对值不跨库可比。
        threshold = self._settings.rag_min_similarity
        if threshold > 0 and max(hit.citation.score for hit in dense) < threshold:
            return []
        reranked = self._rerank(query=query, candidates=self._candidates(dense, lexical, limit=pool))
        if reranked is not None:
            return reranked[:limit]
        return self._fuse_rrf(dense, lexical, limit=limit)

    @staticmethod
    def _candidates(dense: list[KnowledgeHit], lexical: list[KnowledgeHit], *, limit: int) -> list[KnowledgeHit]:
        """两路召回取并集去重。交错着取，一路很长时不会把另一路整个挤掉。"""
        seen: dict[str, KnowledgeHit] = {}
        for pair in zip_longest(dense, lexical):
            for hit in pair:
                if hit is not None and hit.citation.chunk_id not in seen:
                    seen[hit.citation.chunk_id] = hit
        return list(seen.values())[:limit]

    def _rerank(self, *, query: str, candidates: list[KnowledgeHit]) -> list[KnowledgeHit] | None:
        """返回 None 表示重排不可用，调用方退回 RRF。返回空列表表示确实没搜到。"""
        if self._reranker is None or not candidates:
            return None
        scores = self._reranker.rerank(query=query, documents=[hit.content for hit in candidates])
        if scores is None or len(scores) != len(candidates):
            return None
        floor = self._settings.rag_min_rerank_score
        kept = [
            replace(hit, citation=replace(hit.citation, score=round(score, 6)))
            for hit, score in zip(candidates, scores) if score >= floor
        ]
        return sorted(kept, key=lambda hit: hit.citation.score, reverse=True)

    def _dense_search(self, *, course_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        if self._embedder is None:
            return []
        rows = self._repository.load_course_embeddings(course_id=course_id)
        if not rows:
            return []
        try:
            ranked = self._embedder.rank(query=query, vectors=[vector for _, vector in rows], top_k=limit)
        except Exception:
            # 向量维度不一致（换过模型）等异常不应打断检索，退回词面结果。
            return []
        return self._repository.hits_by_chunk_ids(scored=[(rows[index][0], score) for index, score in ranked])

    @staticmethod
    def _fuse_rrf(dense: list[KnowledgeHit], lexical: list[KnowledgeHit], *, limit: int, k: int = 60) -> list[KnowledgeHit]:
        scores: dict[str, float] = {}
        first_seen: dict[str, KnowledgeHit] = {}
        for results in (dense, lexical):
            for rank, hit in enumerate(results, start=1):
                key = hit.citation.chunk_id
                scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
                first_seen.setdefault(key, hit)
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        return [
            replace(first_seen[key], citation=replace(first_seen[key].citation, score=round(score, 6)))
            for key, score in ordered
        ]

    def health(self) -> dict[str, object]:
        try:
            migration_version = self._repository.health_check()
            database: dict[str, object] = {"ok": True, "migration_version": migration_version}
        except Exception as error:
            database = {"ok": False, "error": str(error)}
        rag: dict[str, object] = {"ok": bool(database["ok"]), "backend": "sqlite_fts_fallback"}
        if self._embedder is not None:
            status = self._embedder.status()
            rag["embedding"] = status
            # 向量模型加载失败后只剩词面检索，backend 不能继续报混合。
            rag["backend"] = "sqlite_fts_fallback" if status.get("error") else "hybrid_bge"
        if self._reranker is not None:
            status = self._reranker.status()
            # 重排关掉或加载失败时阈值不生效，这两件事要一起报，否则会以为「查不到就返回空」还在工作。
            rag["reranker"] = {**status, "min_score": self._settings.rag_min_rerank_score}
            if not status.get("error") and rag["backend"] == "hybrid_bge":
                rag["backend"] = "hybrid_bge_rerank"
        return {"database": database, "rag": rag}

    def _run_index(self, job: Job, material: Material) -> Job:
        try:
            self._repository.set_material_status(material.id, "indexing")
            self._repository.update_job(job.id, status="running", stage="extracting", progress=15)
            path = self._repository.material_storage_path(material.id)
            if path is None or not path.is_file():
                raise ValueError("教材文件不存在")
            segments = extract_pages(path, material.filename)
            if self._needs_ocr(material, segments, path):
                # 图片版 PDF 走不通普通提取。停在这里等用户看过账单再确认，不擅自花钱。
                self._repository.set_material_status(material.id, "needs_ocr")
                return self._repository.update_job(
                    job.id, status="failed", stage="needs_ocr", progress=100,
                    error_message="这份 PDF 没有文字层（扫描版）。OCR 要花模型额度，先看估算再确认。",
                )
            if material.ocr_approved and not any(text for _page, text in segments):
                segments = self._ocr_pages(job, path)
            self._repository.update_job(job.id, status="running", stage="chunking", progress=40)
            chunks = [(page, piece) for page, text in segments for piece in self._chunk(text)]
            if not chunks:
                raise ValueError("未能从教材中提取可检索文本")
            embeddings = None
            if self._embedder is not None:
                self._repository.update_job(job.id, status="running", stage="embedding", progress=55)
                # 模型不可用时返回 None，教材仍以纯词面方式完成索引。
                embeddings = self._embedder.embed_documents([content for _page, content in chunks])
            backend = "hybrid_bge" if embeddings else "sqlite_fts"
            self._repository.update_job(job.id, status="running", stage="indexing", progress=85)
            self._repository.replace_chunks(material_id=material.id, course_id=material.course_id, chunks=chunks, embeddings=embeddings)
            # 概念目录是归因的 ID 真源，每次索引完都按同一份文本重跑一次（§8.1）。
            self._repository.update_job(job.id, status="running", stage="concepts", progress=95)
            self._repository.replace_material_concepts(
                course_id=material.course_id, material_id=material.id,
                candidates=extract_candidates([(page, content) for page, content in chunks]),
            )
            self._repository.set_material_status(material.id, "indexed")
            return self._repository.update_job(job.id, status="completed", stage="completed", progress=100, retrieval_backend=backend)
        except Exception as error:
            self._repository.set_material_status(material.id, "failed")
            return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=str(error), retrieval_backend="sqlite_fts")

    def _needs_ocr(self, material: Material, segments: list[tuple[int | None, str]], path: Path) -> bool:
        """判定要不要 OCR：只对没批准过的 PDF、且确实提不出文字时成立。"""
        if material.ocr_approved or Path(material.filename).suffix.lower() != ".pdf":
            return False
        if any(text for _page, text in segments):
            return False
        return self._transcriber is not None and scanned.probe_text_layer(path).is_scanned

    def _ocr_pages(self, job: Job, path: Path) -> list[tuple[int | None, str]]:
        def transcribe(image: bytes) -> str:
            return self._transcriber.transcribe(content=image, mime_type="image/jpeg").plain_text

        def progress(done: int, total: int) -> None:
            # OCR 是整条链路里最慢的一段，进度得走起来，否则界面看着像卡死
            self._repository.update_job(job.id, status="running", stage=f"ocr {done}/{total}", progress=15 + int(20 * done / total))

        pages = [(page, text) for page, text in scanned.transcribe_pages(path, transcribe, on_progress=progress)]
        if not any(text for _page, text in pages):
            raise ValueError("OCR 没有识别出任何文字")
        return pages

    def _chunk(self, text: str) -> list[str]:
        normalized = re.sub(r"\r\n?", "\n", text).strip()
        size, overlap = self._settings.chunk_size, min(self._settings.chunk_overlap, self._settings.chunk_size - 1)
        chunks: list[str] = []
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + size)
            if end < len(normalized):
                split = normalized.rfind("\n", start + max(1, size // 2), end)
                if split > start:
                    end = split
            value = normalized[start:end].strip()
            if value:
                chunks.append(value)
            if end >= len(normalized):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def _material_or_error(self, material_id: str) -> Material:
        material = self._repository.get_material(material_id)
        if material is None:
            raise ValueError("教材不存在")
        return material
