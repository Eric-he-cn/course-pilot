"""RAG retrieval service extracted from runner."""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, List, Tuple

from backend.schemas import RetrievedChunk
from core.errors import IndexNotReadyError
from core.metrics import add_event
from rag.retrieve import Retriever
from rag.store_faiss import FAISSStore


class RAGService:
    """Loads retrievers and returns formatted context for a course/query."""

    def __init__(self, workspace_store):
        self.workspace_store = workspace_store
        self._retriever_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _index_paths(index_path: str) -> Tuple[str, str]:
        return f"{index_path}.faiss", f"{index_path}.pkl"

    @classmethod
    def _cache_version(cls, index_path: str) -> Tuple[float, float] | None:
        faiss_path, pkl_path = cls._index_paths(index_path)
        if not os.path.exists(faiss_path):
            return None
        faiss_mtime = os.path.getmtime(faiss_path)
        pkl_mtime = os.path.getmtime(pkl_path) if os.path.exists(pkl_path) else 0.0
        return float(faiss_mtime), float(pkl_mtime)

    def load_retriever(self, course_name: str) -> Retriever | None:
        workspace_path = self.workspace_store.get_workspace_path(course_name)
        index_path = os.path.abspath(os.path.join(workspace_path, "index", "faiss_index"))
        version = self._cache_version(index_path)
        if version is None:
            return None
        cache_key = f"{course_name}:{index_path}"
        with self._cache_lock:
            cached = self._retriever_cache.get(cache_key)
            if isinstance(cached, dict) and tuple(cached.get("version", ())) == version:
                retriever = cached.get("retriever")
                if retriever is not None:
                    add_event(
                        "retriever_cache",
                        course_name=course_name,
                        cache_hit=True,
                        index_path=index_path,
                    )
                    return retriever
        store = FAISSStore()
        store.load(index_path)
        retriever = Retriever(store)
        with self._cache_lock:
            self._retriever_cache[cache_key] = {"version": version, "retriever": retriever}
        add_event(
            "retriever_cache",
            course_name=course_name,
            cache_hit=False,
            index_path=index_path,
        )
        return retriever

    @staticmethod
    def top_k_for_mode(mode: str) -> int:
        m = (mode or "").strip().lower()
        if m == "exam":
            return int(os.getenv("RAG_TOPK_EXAM", "8"))
        if m in {"learn", "practice"}:
            return int(os.getenv("RAG_TOPK_LEARN_PRACTICE", "4"))
        return int(os.getenv("TOP_K_RESULTS", "3"))

    @staticmethod
    def _evidence_thresholds() -> Dict[str, float]:
        return {
            "dense_min": float(os.getenv("RAG_EVIDENCE_DENSE_MIN", "0.40")),
            "bm25_min": float(os.getenv("RAG_EVIDENCE_BM25_MIN", "1.0")),
            "max_rank": float(os.getenv("RAG_EVIDENCE_MAX_FUSED_RANK", "4")),
        }

    @classmethod
    def _gate_chunks(cls, chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
        thresholds = cls._evidence_thresholds()
        passed: List[RetrievedChunk] = []
        max_rank = max(1, int(thresholds["max_rank"]))
        for rank, chunk in enumerate(list(chunks or []), start=1):
            dense_score = float(chunk.dense_score) if chunk.dense_score is not None else None
            bm25_score = float(chunk.bm25_score) if chunk.bm25_score is not None else None
            dense_ok = dense_score is not None and dense_score >= thresholds["dense_min"]
            bm25_ok = bm25_score is not None and bm25_score >= thresholds["bm25_min"]
            evidence_passed = bool(rank <= max_rank and (dense_ok or bm25_ok))
            gated = chunk.model_copy(update={"evidence_passed": evidence_passed})
            if evidence_passed:
                passed.append(gated)
        return passed

    @staticmethod
    def _query_attempts(question_raw: str, retrieval_query: str) -> List[Tuple[str, str]]:
        primary = str(question_raw or "").strip() or str(retrieval_query or "").strip()
        fallback = str(retrieval_query or "").strip()
        attempts: List[Tuple[str, str]] = []
        if primary:
            attempts.append((primary, "question_raw"))
        if fallback and fallback != primary:
            attempts.append((fallback, "retrieval_query_fallback"))
        return attempts

    def retrieve(
        self,
        *,
        course_name: str,
        question_raw: str = "",
        retrieval_query: str,
        mode: str,
        need_rag: bool = True,
        missing_index_message: str = "（未找到相关教材，请先上传课程资料）",
        empty_message: str = "（检索未命中有效教材片段，本轮将基于已有上下文继续）",
    ) -> Tuple[str, List[RetrievedChunk], bool]:
        if not need_rag:
            add_event(
                "retrieval_skipped",
                course_name=course_name,
                retrieval_query=retrieval_query,
                request_mode=mode or None,
                reason="need_rag_false",
            )
            return "", [], False
        retriever = self.load_retriever(course_name)
        if retriever is None:
            add_event(
                "retrieval_missing_index",
                course_name=course_name,
                retrieval_query=retrieval_query,
                request_mode=mode or None,
            )
            return missing_index_message, [], True
        attempts = self._query_attempts(question_raw, retrieval_query)
        top_k = self.top_k_for_mode(mode)
        last_query = retrieval_query
        for idx, (effective_query, query_source) in enumerate(attempts):
            rewrite_fallback_triggered = idx > 0
            chunks = retriever.retrieve(
                effective_query,
                top_k=top_k,
                mode=mode,
                query_source=query_source,
                rewrite_fallback_triggered=rewrite_fallback_triggered,
            )
            gated_chunks = self._gate_chunks(chunks)
            bm25_scores = [float(chunk.bm25_score or 0.0) for chunk in chunks if chunk.bm25_score is not None]
            dense_passed = 0
            bm25_passed = 0
            thresholds = self._evidence_thresholds()
            for chunk in chunks:
                if chunk.dense_score is not None and float(chunk.dense_score) >= thresholds["dense_min"]:
                    dense_passed += 1
                if chunk.bm25_score is not None and float(chunk.bm25_score) >= thresholds["bm25_min"]:
                    bm25_passed += 1
            add_event(
                "retrieval_evidence_gate",
                course_name=course_name,
                request_mode=mode or None,
                effective_query=effective_query,
                retrieval_query=retrieval_query,
                question_raw=question_raw or None,
                query_source=query_source,
                rewrite_fallback_triggered=rewrite_fallback_triggered,
                candidate_count=len(chunks),
                evidence_passed_count=len(gated_chunks),
                evidence_rejected_count=max(0, len(chunks) - len(gated_chunks)),
                bm25_score_top1=(bm25_scores[0] if bm25_scores else None),
                bm25_score_topk_avg=(sum(bm25_scores) / len(bm25_scores) if bm25_scores else None),
                evidence_passed_by_dense=dense_passed,
                evidence_passed_by_bm25=bm25_passed,
            )
            last_query = effective_query
            if gated_chunks:
                return retriever.format_context(gated_chunks), gated_chunks, False
        add_event(
            "retrieval_unmatched",
            course_name=course_name,
            request_mode=mode or None,
            retrieval_query=retrieval_query,
            question_raw=question_raw or None,
            effective_query=last_query or None,
        )
        return empty_message, [], True

    def require_retriever(self, course_name: str) -> Retriever:
        retriever = self.load_retriever(course_name)
        if retriever is None:
            raise IndexNotReadyError(f"Index missing for course: {course_name}")
        return retriever
