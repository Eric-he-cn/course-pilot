"""
【模块说明】
- 主要作用：执行 RAG 检索并生成带引用信息的上下文文本。
- 核心类：Retriever。
- 核心方法：retrieve（dense/bm25/hybrid 召回）、format_context（拼接引用上下文）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import re
from time import perf_counter
from typing import TYPE_CHECKING, Any, Dict, List, Tuple
from rag.lexical import BM25Index
from rag.rerank import get_reranker_model
from backend.schemas import RetrievedChunk
from core.metrics import add_event

if TYPE_CHECKING:
    from rag.store_faiss import FAISSStore


class Retriever:
    """RAG 检索器（支持引用信息组装）。"""
    """目的：根据查询从 FAISSStore 中检索相关文本片段，并根据配置选择检索模式（dense/bm25/hybrid）。"""
    def __init__(self, store: "FAISSStore"):
        self.store = store
        self._embedding_model = None
        self.lexical_index = BM25Index(
            self.store.chunks,
            k1=self._env_float("BM25_K1", 1.5),
            b=self._env_float("BM25_B", 0.75),
        )

    @staticmethod
    def _rag_compression_mode() -> str:
        raw = str(os.getenv("RAG_COMPRESSION_MODE", "adaptive")).strip().lower()
        if raw in {"adaptive", "always", "off"}:
            return raw
        return "adaptive"

    """_env_int 和 _env_float: 用于从环境变量中读取整数和浮点数配置，提供默认值并确保合理范围。"""
    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, str(default))))
        except Exception:
            return default

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except Exception:
            return default
    """ _chunk_key: 生成唯一的文本片段标识符，用于 RRF 融合时去重和引用。优先使用 chunk_id，
    如果没有则基于 doc_id、page 和文本内容生成一个哈希标识。 """
    @staticmethod
    def _chunk_key(chunk: Dict[str, Any]) -> str:
        if chunk.get("chunk_id"):
            return str(chunk["chunk_id"])
        return f"{chunk.get('doc_id', '')}:{chunk.get('page', '')}:{hash(chunk.get('text', ''))}"

    def _get_embedding_model(self):
        if self._embedding_model is None:
            from rag.embed import get_embedding_model
            self._embedding_model = get_embedding_model()
        return self._embedding_model

    def _dense_search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        query_embedding = self._get_embedding_model().embed_query(query)
        return self.store.search(query_embedding, top_k)

    @staticmethod
    def _rerank_enabled(request_mode: str) -> bool:
        if request_mode not in {"learn", "practice"}:
            return False
        raw = str(os.getenv("RERANK_ENABLED", "1")).strip().lower()
        return raw in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _rerank_candidate_limit(request_mode: str, top_k: int) -> int:
        if request_mode in {"learn", "practice"}:
            return max(top_k, Retriever._env_int("RERANK_CANDIDATES_LEARN_PRACTICE", 12))
        return top_k

    @staticmethod
    def _score_float(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _keywords(query: str) -> List[str]:
        q = (query or "").lower()
        kws = re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", q)
        return list(dict.fromkeys(kws))[:12]

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        parts = re.split(r"(?<=[。！？!?\.])\s+|\n+", text or "")
        return [p.strip() for p in parts if p.strip()]

    def _compress_chunk_text(self, query: str, chunk_text: str) -> str:
        sent_topn = self._env_int("CB_RAG_SENT_PER_CHUNK", 2)
        sent_max_chars = self._env_int("CB_RAG_SENT_MAX_CHARS", 120)
        sents = self._split_sentences(chunk_text)
        if not sents:
            return chunk_text
        kws = self._keywords(query)
        scored = []
        for s in sents:
            low = s.lower()
            overlap = sum(1 for k in kws if k in low)
            scored.append((overlap, len(s), s))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        selected = [x[2] for x in scored[: max(1, sent_topn)]]
        if not any(selected):
            selected = [sents[0]]
        trimmed = [s[: max(30, sent_max_chars)] for s in selected if s]
        if not trimmed:
            return sents[0][: max(30, sent_max_chars)]
        return " ".join(trimmed)


    """_fuse_with_rrf: 使用 Reciprocal Rank Fusion (RRF) 算法融合 dense 和 bm25 的检索结果。
    通过给每个结果根据其在两个列表中的排名分配权重，最终得到一个综合评分并排序。"""
    def _fuse_with_rrf(
        self,
        dense_results: List[Tuple[Dict[str, Any], float]],
        bm25_results: List[Tuple[Dict[str, Any], float]],
        limit: int | None = None,
    ) -> List[Dict[str, Any]]:
        rrf_k = self._env_int("HYBRID_RRF_K", 60)
        dense_weight = self._env_float("HYBRID_DENSE_WEIGHT", 1.0)
        bm25_weight = self._env_float("HYBRID_BM25_WEIGHT", 1.0)

        fused_scores: Dict[str, float] = {}
        chunk_ref: Dict[str, Dict[str, Any]] = {}
        dense_score_map: Dict[str, float] = {}
        bm25_score_map: Dict[str, float] = {}

        for rank, (chunk, _) in enumerate(dense_results, start=1):
            key = self._chunk_key(chunk)
            fused_scores[key] = fused_scores.get(key, 0.0) + dense_weight / (rrf_k + rank)
            chunk_ref[key] = chunk
            dense_score_map[key] = self._score_float(_)
            bm25_score_map.setdefault(key, None)

        for rank, (chunk, _) in enumerate(bm25_results, start=1):
            key = self._chunk_key(chunk)
            fused_scores[key] = fused_scores.get(key, 0.0) + bm25_weight / (rrf_k + rank)
            chunk_ref[key] = chunk
            bm25_score_map[key] = self._score_float(_)
            dense_score_map.setdefault(key, None)

        ranked = sorted(
            ((key, score) for key, score in fused_scores.items()),
            key=lambda x: x[1],
            reverse=True,
        )
        fused: List[Dict[str, Any]] = []
        selected = ranked if limit is None else ranked[: max(1, int(limit))]
        for rank, (key, score) in enumerate(selected, start=1):
            fused.append(
                {
                    "chunk": chunk_ref[key],
                    "score": float(score),
                    "rrf_score": float(score),
                    "dense_score": dense_score_map.get(key),
                    "bm25_score": bm25_score_map.get(key),
                    "rank": rank,
                }
            )
        return fused

    def _rerank_candidates(
        self,
        *,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        reranker = get_reranker_model()
        texts = [str(item.get("chunk", {}).get("text", "") or "") for item in candidates]
        scores = reranker.rerank(query=query, texts=texts)
        reranked: List[Dict[str, Any]] = []
        for item, rerank_score in zip(candidates, scores):
            updated = dict(item)
            updated["score"] = float(rerank_score)
            updated["rerank_score"] = float(rerank_score)
            reranked.append(updated)
        reranked.sort(key=lambda x: float(x.get("rerank_score", x.get("score", 0.0)) or 0.0), reverse=True)
        for rank, item in enumerate(reranked, start=1):
            item["rank"] = rank
        return reranked[: max(1, int(top_k))]
    

    """retrieve: 根据查询执行检索，支持 dense、bm25 和 hybrid 三种模式。根据环境变量配置选择模式和参数，"""
    def retrieve(
        self,
        query: str,
        top_k: int = None,
        mode: str = "",
        query_source: str = "",
        rewrite_fallback_triggered: bool = False,
    ) -> List[RetrievedChunk]:
        """根据查询召回相关文本片段。"""
        t0 = perf_counter()
        search_mode = "hybrid"
        request_mode = str(mode or "").strip().lower()
        candidate_count = 0
        success = True
        rerank_enabled = self._rerank_enabled(request_mode)
        rerank_applied = False
        rerank_candidate_count = 0
        rerank_returned_count = 0
        rerank_ms = 0.0
        rerank_error = ""
        if top_k is None:
            top_k = self._env_int("TOP_K_RESULTS", 3)
        else:
            top_k = max(1, int(top_k))

        search_mode = os.getenv("RETRIEVAL_MODE", "hybrid").strip().lower()
        if search_mode not in {"dense", "bm25", "hybrid"}:
            search_mode = "hybrid"

        try:
            if search_mode == "dense":
                results = self._dense_search(query, top_k)
                candidate_count = len(results)
                scored_results = [
                    {
                        "chunk": chunk,
                        "score": float(score),
                        "dense_score": float(score),
                        "bm25_score": None,
                        "rrf_score": None,
                        "rank": rank,
                    }
                    for rank, (chunk, score) in enumerate(results, start=1)
                ]
            elif search_mode == "bm25":
                results = self.lexical_index.search(query, top_k)
                candidate_count = len(results)
                scored_results = [
                    {
                        "chunk": chunk,
                        "score": float(score),
                        "dense_score": None,
                        "bm25_score": float(score),
                        "rrf_score": None,
                        "rank": rank,
                    }
                    for rank, (chunk, score) in enumerate(results, start=1)
                ]
            else:
                if request_mode in {"learn", "practice"}:
                    dense_k = max(top_k, 10)
                    bm25_k = max(top_k, 10)
                elif request_mode == "exam":
                    dense_k = max(top_k, 16)
                    bm25_k = max(top_k, 16)
                else:
                    dense_multiplier = self._env_int("HYBRID_DENSE_CANDIDATES_MULTIPLIER", 3)
                    bm25_multiplier = self._env_int("HYBRID_BM25_CANDIDATES_MULTIPLIER", 3)
                    dense_k = max(top_k, top_k * dense_multiplier)
                    bm25_k = max(top_k, top_k * bm25_multiplier)
                dense_results = self._dense_search(query, dense_k)
                bm25_results = self.lexical_index.search(query, bm25_k)
                candidate_count = len(dense_results) + len(bm25_results)
                fused_limit = self._rerank_candidate_limit(request_mode, top_k) if rerank_enabled else top_k
                fused_results = self._fuse_with_rrf(dense_results, bm25_results, limit=fused_limit)
                scored_results = fused_results[:top_k]
                if rerank_enabled and fused_results:
                    rerank_candidate_count = len(fused_results)
                    rerank_t0 = perf_counter()
                    try:
                        scored_results = self._rerank_candidates(
                            query=query,
                            candidates=fused_results,
                            top_k=top_k,
                        )
                        rerank_applied = True
                        rerank_returned_count = len(scored_results)
                    except Exception as ex:
                        rerank_error = f"{type(ex).__name__}: {ex}"
                        scored_results = fused_results[:top_k]
                    finally:
                        rerank_ms = (perf_counter() - rerank_t0) * 1000.0
        except Exception:
            success = False
            raise
        
        compression_mode = self._rag_compression_mode()
        sentence_compress_applied = compression_mode == "always"
        retrieved = []
        for item in scored_results:
            chunk = item["chunk"]
            score = float(item["score"])
            if compression_mode == "always":
                # 明确开启时，检索层直接句级压缩。
                comp_text = self._compress_chunk_text(query=query, chunk_text=chunk["text"])
            else:
                # adaptive/off 模式都在预算层决定是否二次压缩，检索层保留完整 chunk。
                comp_text = str(chunk.get("text", "") or "")
            retrieved.append(RetrievedChunk(
                text=comp_text,
                doc_id=chunk["doc_id"],
                page=chunk.get("page"),
                chunk_id=chunk.get("chunk_id"),
                score=score,
                dense_score=self._score_float(item.get("dense_score")),
                bm25_score=self._score_float(item.get("bm25_score")),
                rrf_score=self._score_float(item.get("rrf_score")),
                rerank_score=self._score_float(item.get("rerank_score")),
            ))

        add_event(
            "retrieval",
            retrieval_ms=(perf_counter() - t0) * 1000.0,
            mode=search_mode,
            request_mode=request_mode or None,
            top_k=top_k,
            candidate_count=candidate_count,
            returned_count=len(retrieved),
            effective_query=query,
            query_source=query_source or None,
            rewrite_fallback_triggered=bool(rewrite_fallback_triggered),
            rag_compression_mode=compression_mode,
            rag_sentence_compress_applied=sentence_compress_applied,
            rerank_enabled=rerank_enabled if search_mode == "hybrid" else False,
            rerank_applied=rerank_applied,
            rerank_ms=rerank_ms if rerank_applied or rerank_error else None,
            rerank_candidate_count=rerank_candidate_count if rerank_enabled else None,
            rerank_returned_count=rerank_returned_count if rerank_applied else None,
            rerank_error=rerank_error or None,
            rerank_fallback=bool(rerank_error),
            success=success,
        )
        return retrieved
    
    def format_context(self, chunks: List[RetrievedChunk]) -> str:
        """把检索片段格式化为可直接注入 LLM 的上下文字符串。"""
        context_parts = []
        for i, chunk in enumerate(chunks):
            citation = f"[来源{i+1}: {chunk.doc_id}"
            if chunk.page:
                citation += f", 第{chunk.page}页"
            citation += "]"
            
            context_parts.append(f"{citation}\n{chunk.text}\n")
        
        return "\n".join(context_parts)
