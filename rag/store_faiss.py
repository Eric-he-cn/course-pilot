"""
【模块说明】
- 主要作用：封装 FAISS 向量索引的增删查与持久化。
- 核心类：FAISSStore。
- 核心函数：build_index（从文本块构建索引）。
"""
import os
import pickle
import threading
from typing import List, Dict, Any, Tuple
import faiss
import numpy as np
from rag.embed import get_embedding_model

# Windows 下 FAISS C++ 的 fopen 不支持 Unicode 路径，只能 chdir 绕过。
# 用全局锁确保并发请求不互相干扰 os.chdir。
_faiss_chdir_lock = threading.Lock()


class FAISSStore:
    """基于 FAISS 的向量存储封装。"""
    
    def __init__(self, dimension: int = 384):
        self.dimension = dimension
        self.index = faiss.IndexFlatL2(dimension)
        self.chunks = []
    
    def add_chunks(self, chunks: List[Dict[str, Any]], embeddings: np.ndarray):
        """向索引中添加文本块及其向量。"""
        self.index.add(embeddings.astype('float32'))
        self.chunks.extend(chunks)
    
    def search(self, query_embedding: np.ndarray, top_k: int = 3) -> List[Tuple[Dict[str, Any], float]]:
        """检索与查询向量最相近的文本块。"""
        query_embedding = query_embedding.astype('float32').reshape(1, -1)
        distances, indices = self.index.search(query_embedding, top_k)
        
        results = []
        for i, idx in enumerate(indices[0]):
            if idx < len(self.chunks):
                # 将 L2 距离转换为相似度分数（反比）
                score = 1.0 / (1.0 + distances[0][i])
                results.append((self.chunks[idx], score))
        
        return results
    
    def save(self, path: str):
        """把索引与文本块元数据保存到磁盘。"""
        path = os.path.abspath(path)
        index_dir = os.path.dirname(path)
        filename = os.path.basename(path)
        os.makedirs(index_dir, exist_ok=True)
        # FAISS C++ 底层 fopen 在 Windows 上不支持 Unicode 路径，
        # 切换到目标目录后用纯 ASCII 相对路径写入
        with _faiss_chdir_lock:
            cwd = os.getcwd()
            try:
                os.chdir(index_dir)
                faiss.write_index(self.index, f"{filename}.faiss")
            finally:
                os.chdir(cwd)
        with open(f"{path}.pkl", 'wb') as f:
            pickle.dump(self.chunks, f)
    
    def load(self, path: str):
        """从磁盘加载索引与文本块元数据。"""
        path = os.path.abspath(path)
        index_dir = os.path.dirname(path)
        filename = os.path.basename(path)
        with _faiss_chdir_lock:
            cwd = os.getcwd()
            try:
                os.chdir(index_dir)
                self.index = faiss.read_index(f"{filename}.faiss")
            finally:
                os.chdir(cwd)
        with open(f"{path}.pkl", 'rb') as f:
            self.chunks = pickle.load(f)
    
    @property
    def size(self) -> int:
        """返回当前索引中的向量数量。"""
        return self.index.ntotal


def build_index(chunks: List[Dict[str, Any]]) -> FAISSStore:
    """根据文本块构建并返回 FAISSStore。"""
    embedding_model = get_embedding_model()
    texts = [chunk["text"] for chunk in chunks]
    embeddings = embedding_model.embed(texts)
    
    store = FAISSStore(dimension=embeddings.shape[1])
    store.add_chunks(chunks, embeddings)
    
    return store
