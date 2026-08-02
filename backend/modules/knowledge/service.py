from __future__ import annotations

import logging
import re
from dataclasses import replace
from itertools import zip_longest
from pathlib import Path
from typing import Callable

from core.common import new_id, utc_now
from core.settings import Settings
from contracts.embedding import EmbedderPort
from contracts.knowledge import ConceptRef, KnowledgeHit, ResolvedKnowledgeScope, WikiDocument, WikiEntry
from contracts.llm import ChatFinal
from contracts.reranker import RerankerPort

from .api import KnowledgeFeatureDisabledError, MaterialNotIndexedError
from .concepts import extract_candidates, from_outline
from . import scanned, wiki
from .extract import SUPPORTED_SUFFIXES, extract_pages, pdf_outline
from .wiki import WikiStore
from .models import STAGE_INDEX_DONE, ConceptNode, Job, Material
from .repository import KnowledgeRepository

_LOG = logging.getLogger(__name__)


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
        wiki_store: WikiStore | None = None,
        responder: object | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._wiki_is_enabled = wiki_is_enabled or (lambda _course_id: False)
        self._embedder = embedder
        self._reranker = reranker
        self._transcriber = transcriber
        self._wiki = wiki_store
        self._responder = responder

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
            return self._run_upload_pipelines(job, material)
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

    def _run_upload_pipelines(self, job: Job, material: Material) -> Job:
        """一次上传跑两条流水线：先检索索引，再目录结构，用户不必点第二次。

        两条都收工才把作业记成完成——界面靠这一下刷新概念目录，早一步就会显示上一版。
        """
        indexed = self._run_index(job, material)
        if indexed.stage != STAGE_INDEX_DONE:  # 索引失败，或停在等 OCR 确认
            return indexed
        self._parse_structure_quietly(material)
        return self._repository.update_job(job.id, status="completed", stage="completed", progress=100)

    # ---- 目录结构：概念与层级。和检索索引共享文本准备，之后各走各的 ----

    def structure_status(self, *, course_id: str) -> list[dict[str, object]]:
        """每份教材的目录结构状态。有没有概念、有没有层级都从 concepts 表现算，不存状态列。"""
        return [
            {**row, "has_structure": row["concepts"] > 0, "has_levels": row["leveled"] > 0}
            for row in self._repository.material_concept_stats(course_id=course_id)
        ]

    def preview_structure(self, *, material_id: str) -> dict[str, object]:
        """重建目录结构之前的影响预告。删概念会连带删掉掌握度与错题，用户有权先看见。"""
        material = self._material_or_error(material_id)
        return {
            **self._repository.preview_material_concepts(
                course_id=material.course_id, material_id=material.id,
                candidates=self._structure_candidates(material)),
            "material_id": material.id,
        }

    def parse_structure(self, *, material_id: str) -> dict[str, object]:
        """重算这份教材的概念与层级。不重新提取、不重新向量化，chunks 一行都不动。

        亚秒级，所以是同步接口，不进 jobs 表。
        """
        material = self._material_or_error(material_id)
        candidates = self._structure_candidates(material)
        # 预告与执行用同一份候选，报出的数字就是真正发生的事。
        applied = self._repository.preview_material_concepts(
            course_id=material.course_id, material_id=material.id, candidates=candidates)
        self._repository.replace_material_concepts(
            course_id=material.course_id, material_id=material.id, candidates=candidates)
        current = next((row for row in self.structure_status(course_id=material.course_id)
                        if row["material_id"] == material.id), {})
        return {**applied, **current, "material_id": material.id}

    def _structure_candidates(self, material: Material) -> list[dict]:
        """目录结构只吃文本准备的产物：有书签的 PDF 读文件，没书签的读已落库的 chunk 正文。"""
        chunks = self._repository.list_material_chunks(material_id=material.id)
        if not chunks:
            raise MaterialNotIndexedError("这份教材还没有可读的正文，先重建索引")
        return self._concepts_for(
            self._repository.material_storage_path(material.id), material.filename,
            [(row["page"], row["content"]) for row in chunks],
        )

    def _parse_structure_quietly(self, material: Material) -> None:
        """结构解析失败不该把检索索引一起拖垮。它随时可以单独重算，检索却要重跑向量化。"""
        try:
            self.parse_structure(material_id=material.id)
        except Exception as error:
            _LOG.warning("目录结构解析失败，检索索引不受影响：%s", error)

    def estimate_wiki(self, *, material_id: str) -> dict[str, object]:
        """知识页构建前的账单。离线跑一次 plan_sections 数页数，一次模型调用都不发。"""
        material = self._material_or_error(material_id)
        if not self._wiki_is_enabled(material.course_id):
            raise KnowledgeFeatureDisabledError("该课程尚未启用 Wiki")
        if material.index_status != "indexed":
            raise MaterialNotIndexedError("教材尚未完成索引")
        chunks = self._repository.list_material_chunks(material_id=material.id)
        if not chunks:
            raise MaterialNotIndexedError("这份教材还没有可读的正文，先重建索引")
        concepts = self._repository.list_material_concept_tree(material_id=material.id)
        sections, stats = wiki.plan_sections(material_id=material.id, chunks=chunks, concepts=concepts)
        pages = len(sections) + 1  # 课程首页
        seconds = pages * wiki.SECONDS_PER_PAGE
        return {"pages": pages, "calls": pages, "seconds": seconds, "minutes": round(seconds / 60, 1),
                "sections": len(sections), "candidates": stats["candidates"], "merged": stats["capped"],
                "has_levels": any(row.get("level") is not None for row in concepts)}

    def _run_wiki(self, job: Job, material: Material) -> Job:
        """按教材目录自底向上把整份教材写成一棵知识页，全程不走检索。

        叶子页读它那一节页码范围内的全部原文，中间页读子页，最后写课程首页。
        """
        try:
            self._repository.update_job(job.id, status="running", stage="reading_index", progress=10)
            if self._wiki is None or self._responder is None:
                raise ValueError("Wiki 需要配好模型槽位（见 .env 的 TEXT_*）")
            chunks = self._repository.list_material_chunks(material_id=material.id)
            if not chunks:
                raise ValueError("这份教材还没有可读的正文，先重建索引")
            sections, stats = wiki.plan_sections(
                material_id=material.id, chunks=chunks,
                concepts=self._repository.list_material_concept_tree(material_id=material.id),
            )
            if not sections:
                raise ValueError("这份教材切不出可写的小节，先重建索引")

            def progress(done: int, total: int) -> None:
                self._repository.update_job(job.id, status="running", stage=f"wiki {done}/{total}", progress=10 + int(85 * done / total))

            # 概念列表可能在上次重建索引时变过，先把孤儿页清掉再生成
            orphans = self._wiki.prune(
                course_id=material.course_id,
                valid_concept_ids=self._repository.concept_ids(course_id=material.course_id),
                material_id=material.id, planned_ids={section.id for section in sections},
                known_material_ids=self._repository.material_ids(course_id=material.course_id),
            )
            counts = wiki.build_pages(
                course_id=material.course_id, material_id=material.id, document=material.filename,
                sections=sections, store=self._wiki, now=utc_now(), ask=self._ask_once, on_progress=progress,
            )
            self.reindex_wiki_pages(course_id=material.course_id, material_id=material.id)
            summary = wiki.coverage_summary({**counts, **stats, "pruned": len(orphans)})
            return self._repository.update_job(job.id, status="completed", stage="wiki_completed", progress=100, error_message=summary)
        except Exception as error:
            return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=str(error))

    def _ask_once(self, messages: list) -> object:
        """把流式接口收成一次问答：Wiki 不需要边写边显示。"""
        final = None
        for event in self._responder.chat(messages=messages):
            if isinstance(event, ChatFinal):
                final = event
        if final is None:
            raise ValueError("模型没有返回完整结果")
        return final

    def reindex_wiki_pages(self, *, course_id: str, material_id: str) -> int:
        """把这门课落盘的每一页知识页正文写成一行可检索记录，整课替换。

        知识页也要能被引用，前提是它得先在检索库里。归属教材照页里记的来，
        记不到的（课程首页）挂在触发这次构建的那份上，删教材时才有人收走它。
        """
        if self._wiki is None:
            return 0
        known = self._repository.material_ids(course_id=course_id)
        pages = []
        for page in self._wiki.list_pages(course_id=course_id):
            document = wiki.split_page(
                concept_id=page.concept_id,
                document=self._wiki.read(course_id=course_id, concept_id=page.concept_id),
            )
            body = document.body.strip()
            if not body:
                continue
            owner = page.material_id if page.material_id in known else material_id
            pages.append({"concept_id": page.concept_id, "concept_name": document.concept_name,
                          "material_id": owner, "content": f"{document.concept_name}\n\n{body}"})
        embeddings = None
        if self._embedder is not None and pages:
            embeddings = self._embedder.embed_documents([page["content"] for page in pages])
        self._repository.replace_wiki_chunks(course_id=course_id, pages=pages, embeddings=embeddings)
        return len(pages)

    def wiki_pages(self, *, course_id: str) -> list[dict[str, object]]:
        """层级一并下发：页面 frontmatter 里本来就记着，丢在服务层界面就画不出树。"""
        if self._wiki is None:
            return []
        return [
            {"concept_id": page.concept_id, "concept_name": page.concept_name,
             "updated_at": page.updated_at, "chars": page.chars,
             "parent_id": page.parent_id, "level": page.level, "order": page.order}
            for page in self._wiki.list_pages(course_id=course_id)
        ]

    def wiki_page(self, *, course_id: str, concept_id: str) -> str:
        if self._wiki is None:
            raise LookupError(concept_id)
        return self._wiki.read(course_id=course_id, concept_id=concept_id)

    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]:
        """Agent-only search: the course is a server-issued resolver result."""
        return self.search_course(course_id=scope.course_id, query=query, limit=limit)

    def search_wiki(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 2) -> list[KnowledgeHit]:
        """Agent-only：知识页那一路，名额与教材那一路各算各的。

        不和教材合排：知识页用概括的语言写、提问也是概括的语言，同一个列表比相似度
        它会占便宜，把教材原文挤出去，结果是照着转述回答。
        """
        if not query.strip() or not self._wiki_is_enabled(scope.course_id):
            return []
        limit = max(1, min(limit, 10))
        lexical = self._repository.search_wiki(course_id=scope.course_id, query=query, limit=limit * 3)
        dense = self._dense_wiki(course_id=scope.course_id, query=query, limit=limit * 3)
        if not dense:
            return lexical[:limit]
        reranked = self._rerank(query=query, candidates=self._candidates(dense, lexical, limit=limit * 3))
        if reranked is not None:
            return reranked[:limit]
        return self._fuse_rrf(dense, lexical, limit=limit)

    def _dense_wiki(self, *, course_id: str, query: str, limit: int) -> list[KnowledgeHit]:
        if self._embedder is None:
            return []
        rows = self._repository.load_course_embeddings(course_id=course_id, source_kind="wiki")
        if not rows:
            return []
        try:
            ranked = self._embedder.rank(query=query, vectors=[vector for _, vector in rows], top_k=limit)
        except Exception:
            return []
        return self._repository.wiki_hits_by_ids(scored=[(rows[index][0], score) for index, score in ranked])

    def material_names(self, *, scope: ResolvedKnowledgeScope) -> list[str]:
        return [material.filename for material in self.list_materials(course_id=scope.course_id)]

    def concepts(self, *, scope: ResolvedKnowledgeScope, limit: int = 60) -> list[ConceptRef]:
        return self.list_course_concepts(course_id=scope.course_id, limit=limit)

    def wiki_enabled(self, *, scope: ResolvedKnowledgeScope) -> bool:
        return self._wiki_is_enabled(scope.course_id)

    def wiki_index(self, *, scope: ResolvedKnowledgeScope) -> list[WikiEntry]:
        """Agent-only：课程没开知识页就当作没有页，不靠调用方记得先问一句。"""
        if not self._wiki_is_enabled(scope.course_id):
            return []
        return [
            WikiEntry(str(page["concept_id"]), str(page["concept_name"]), int(page["chars"] or 0))
            for page in self.wiki_pages(course_id=scope.course_id)
        ]

    def wiki_read(self, *, scope: ResolvedKnowledgeScope, concept_id: str) -> WikiDocument:
        """Agent-only：按落盘格式拆成正文与手写区。frontmatter 是内部记账（证据指纹、
        提示词版本），对调用方没有意义，就地丢掉。"""
        if not self._wiki_is_enabled(scope.course_id):
            raise LookupError(concept_id)
        raw = self.wiki_page(course_id=scope.course_id, concept_id=concept_id)
        return wiki.split_page(concept_id=concept_id, document=raw)

    def concept_exists(self, course_id: str, concept_id: str) -> bool:
        """按 id 精确判断，不受概念清单的展示上限影响。"""
        return self._repository.concept_exists(course_id=course_id, concept_id=concept_id)

    def concept_tree(self, *, course_id: str) -> list[ConceptNode]:
        """整份概念目录，按教材目录顺序返回。父子关系用 parent_id 表示，调用方自己嵌套。"""
        return [
            ConceptNode(id=row["id"], name=row["name"], page=row["page"], level=row["level"],
                        parent_id=row["parent_id"], material_id=row["material_id"])
            for row in self._repository.list_concept_tree(course_id=course_id)
        ]

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
        # 本机探测结果一并上报：弱机器上模型选型是自动降档的，看不到就没法判断慢是不是这个原因。
        if self._settings.hardware:
            rag["hardware"] = self._settings.hardware
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
        """检索索引这一路：提取 → 切块 → 向量化 → 写 chunks。概念与层级不在这里，
        它们由目录结构那一路单独重算（见 parse_structure）。"""
        try:
            self._repository.set_material_status(material.id, "indexing")
            self._repository.update_job(job.id, status="running", stage="extracting", progress=15)
            path = self._repository.material_storage_path(material.id)
            if path is None or not path.is_file():
                raise ValueError("教材文件不存在")
            segments = extract_pages(path, material.filename)
            scanned_pdf = self._is_scanned_pdf(material, path)
            if scanned_pdf and not material.ocr_approved:
                # 图片版 PDF 走不通普通提取。停在这里等用户看过账单再确认，不擅自花钱。
                self._repository.set_material_status(material.id, "needs_ocr")
                return self._repository.update_job(
                    job.id, status="failed", stage="needs_ocr", progress=100,
                    error_message="这份 PDF 没有文字层（扫描版）。OCR 要花模型额度，先看估算再确认。",
                )
            if scanned_pdf and material.ocr_approved:
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
            self._repository.set_material_status(material.id, "indexed")
            # 停在 running 交给调用方收尾：目录结构那一路还要跑，作业不能在这里就报完成。
            return self._repository.update_job(job.id, status="running", stage="structure", progress=95, retrieval_backend=backend)
        except Exception as error:
            self._repository.set_material_status(material.id, "failed")
            return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=str(error), retrieval_backend="sqlite_fts")

    def _concepts_for(self, path: Path | None, filename: str, chunks: list[tuple[int | None, str]]) -> list[dict]:
        """有目录书签就用它，没有才从正文刮标题。

        刮标题在代码和表格多的教材上假阳性很高——markdown 标题正则会命中 Python 注释，
        编号标题正则会命中表格行，页码还常常指到目录页。书签是作者写的，这些问题都没有。
        """
        if path is not None and path.is_file() and Path(filename).suffix.lower() == ".pdf":
            candidates = from_outline(pdf_outline(path))
            if candidates:
                return candidates
        return extract_candidates([(page, content) for page, content in chunks])

    def _is_scanned_pdf(self, material: Material, path: Path) -> bool:
        """扫描版判定只认文字层的页中位数（见 scanned.TEXT_LAYER_MIN_CHARS）。
        「提出任意文字就算文字版」会漏掉带页码或文字封面的扫描件。"""
        if Path(material.filename).suffix.lower() != ".pdf" or self._transcriber is None:
            return False
        return scanned.probe_text_layer(path).is_scanned

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
