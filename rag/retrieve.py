"""
【模块说明】
- 主要作用：执行向量检索并生成带引用信息的上下文文本。
- 核心类：Retriever。
- 核心方法：retrieve（召回片段）、format_context（拼接引用上下文）。
"""
import os
from typing import List
from rag.store_faiss import FAISSStore
from rag.embed import get_embedding_model
from backend.schemas import RetrievedChunk


class Retriever:
    """RAG 检索器（支持引用信息组装）。"""
    
    def __init__(self, store: FAISSStore):
        self.store = store
        self.embedding_model = get_embedding_model()
    
    def retrieve(
        self,
        query: str,
        top_k: int = None
    ) -> List[RetrievedChunk]:
        """根据查询召回相关文本片段。"""
        if top_k is None:
            top_k = int(os.getenv("TOP_K_RESULTS", "3"))
        
        query_embedding = self.embedding_model.embed_query(query)
        results = self.store.search(query_embedding, top_k)
        
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
