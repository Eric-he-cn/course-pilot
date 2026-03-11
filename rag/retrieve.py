"""
【模块说明】
- 主要作用：执行 RAG 检索并生成带引用信息的上下文文本。
- 核心类：Retriever。
- 核心方法：retrieve（dense/bm25/hybrid 召回）、format_context（拼接引用上下文）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
from typing import TYPE_CHECKING, Any, Dict, List, Tuple
from rag.lexical import BM25Index
from backend.schemas import RetrievedChunk

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


    """_fuse_with_rrf: 使用 Reciprocal Rank Fusion (RRF) 算法融合 dense 和 bm25 的检索结果。
    通过给每个结果根据其在两个列表中的排名分配权重，最终得到一个综合评分并排序。"""
    def _fuse_with_rrf(
        self,
        dense_results: List[Tuple[Dict[str, Any], float]],
        bm25_results: List[Tuple[Dict[str, Any], float]],
        top_k: int,
    ) -> List[Tuple[Dict[str, Any], float]]:
        rrf_k = self._env_int("HYBRID_RRF_K", 60)
        dense_weight = self._env_float("HYBRID_DENSE_WEIGHT", 1.0)
        bm25_weight = self._env_float("HYBRID_BM25_WEIGHT", 1.0)

        fused_scores: Dict[str, float] = {}
        chunk_ref: Dict[str, Dict[str, Any]] = {}

        for rank, (chunk, _) in enumerate(dense_results, start=1):
            key = self._chunk_key(chunk)
            fused_scores[key] = fused_scores.get(key, 0.0) + dense_weight / (rrf_k + rank)
            chunk_ref[key] = chunk

        for rank, (chunk, _) in enumerate(bm25_results, start=1):
            key = self._chunk_key(chunk)
            fused_scores[key] = fused_scores.get(key, 0.0) + bm25_weight / (rrf_k + rank)
            chunk_ref[key] = chunk

        ranked = sorted(
            ((key, score) for key, score in fused_scores.items()),
            key=lambda x: x[1],
            reverse=True,
        )
        return [(chunk_ref[key], float(score)) for key, score in ranked[:top_k]]
    

    """retrieve: 根据查询执行检索，支持 dense、bm25 和 hybrid 三种模式。根据环境变量配置选择模式和参数，"""
    def retrieve(
        self,
        query: str,
        top_k: int = None
    ) -> List[RetrievedChunk]:
        """根据查询召回相关文本片段。"""
        if top_k is None:
            top_k = self._env_int("TOP_K_RESULTS", 3)
        else:
            top_k = max(1, int(top_k))

        mode = os.getenv("RETRIEVAL_MODE", "hybrid").strip().lower()
        if mode not in {"dense", "bm25", "hybrid"}:
            mode = "hybrid"

        if mode == "dense":
            results = self._dense_search(query, top_k)
        elif mode == "bm25":
            results = self.lexical_index.search(query, top_k)
        else:
            dense_multiplier = self._env_int("HYBRID_DENSE_CANDIDATES_MULTIPLIER", 3)
            bm25_multiplier = self._env_int("HYBRID_BM25_CANDIDATES_MULTIPLIER", 3)
            dense_k = max(top_k, top_k * dense_multiplier)
            bm25_k = max(top_k, top_k * bm25_multiplier)
            dense_results = self._dense_search(query, dense_k)
            bm25_results = self.lexical_index.search(query, bm25_k)
            results = self._fuse_with_rrf(dense_results, bm25_results, top_k)
        
        retrieved = []
        for chunk, score in results:
            retrieved.append(RetrievedChunk(
                text=chunk["text"],
                doc_id=chunk["doc_id"],
                page=chunk.get("page"),
                chunk_id=chunk.get("chunk_id"),
                score=float(score)
            ))
        
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
