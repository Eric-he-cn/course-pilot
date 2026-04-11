"""RAG retrieval service extracted from runner."""

from __future__ import annotations

import os
from typing import List, Tuple

from backend.schemas import RetrievedChunk
from core.errors import IndexNotReadyError
from rag.retrieve import Retriever
from rag.store_faiss import FAISSStore


class RAGService:
    """Loads retrievers and returns formatted context for a course/query."""

    def __init__(self, workspace_store):
        self.workspace_store = workspace_store

    def load_retriever(self, course_name: str) -> Retriever | None:
        workspace_path = self.workspace_store.get_workspace_path(course_name)
        index_path = os.path.abspath(os.path.join(workspace_path, "index", "faiss_index"))
        if not os.path.exists(f"{index_path}.faiss"):
            return None
        store = FAISSStore()
        store.load(index_path)
        return Retriever(store)

    @staticmethod
    def top_k_for_mode(mode: str) -> int:
        m = (mode or "").strip().lower()
        if m == "exam":
            return int(os.getenv("RAG_TOPK_EXAM", "8"))
        if m in {"learn", "practice"}:
            return int(os.getenv("RAG_TOPK_LEARN_PRACTICE", "4"))
        return int(os.getenv("TOP_K_RESULTS", "3"))

    def retrieve(
        self,
        *,
        course_name: str,
        retrieval_query: str,
        mode: str,
        need_rag: bool = True,
        missing_index_message: str = "（未找到相关教材，请先上传课程资料）",
        empty_message: str = "（检索未命中有效教材片段，本轮将基于已有上下文继续）",
    ) -> Tuple[str, List[RetrievedChunk], bool]:
        if not need_rag:
            return "", [], False
        retriever = self.load_retriever(course_name)
        if retriever is None:
            return missing_index_message, [], True
        chunks = retriever.retrieve(retrieval_query, top_k=self.top_k_for_mode(mode), mode=mode)
        if not chunks:
            return empty_message, [], True
        return retriever.format_context(chunks), chunks, False

    def require_retriever(self, course_name: str) -> Retriever:
        retriever = self.load_retriever(course_name)
        if retriever is None:
            raise IndexNotReadyError(f"Index missing for course: {course_name}")
        return retriever
