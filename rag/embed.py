"""
【模块说明】
- 主要作用：封装嵌入模型加载与向量生成（文档向量 + 查询向量）。
- 核心类：EmbeddingModel。
- 核心函数：get_embedding_model（全局单例获取）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
from typing import List
from sentence_transformers import SentenceTransformer
import numpy as np
import torch

# BGE 系列检索模型在 encode 查询时需要加指令前缀（文档片段不需要）。
# bge-*-zh-*  → 中文指令前缀
# bge-m3      → 无需前缀（模型内置多语言 instruction tuning）
_BGE_ZH_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def _get_bge_query_prefix(model_name: str) -> str:
    """返回 BGE 模型查询前缀（不需要时返回空字符串）。"""
    name = model_name.lower()
    if "bge-m3" in name:
        return ""          # bge-m3 不需要前缀
    if "bge" in name and ("zh" in name or "chinese" in name):
        return _BGE_ZH_QUERY_INSTRUCTION
    return ""


def _select_device() -> str:
    """自动选择计算设备。

    优先读取 EMBEDDING_DEVICE 环境变量：
      - "auto" (默认) → 有 CUDA 用 cuda:0，否则 cpu
      - "cuda" / "cuda:0" → 强制使用 GPU
      - "cpu"             → 强制使用 CPU
    """
    env = os.getenv("EMBEDDING_DEVICE", "auto").strip().lower()
    if env != "auto":
        return env
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        print(f"[Embed] 使用 GPU: {gpu_name}")
        return "cuda"
    print("[Embed] 未检测到 CUDA，使用 CPU")
    return "cpu"


class EmbeddingModel:
    """Sentence-Transformer 嵌入模型封装（含设备自动选择与查询前缀处理）。"""

    def __init__(self, model_name: str = None):
        if model_name is None:
            model_name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-zh-v1.5")
        self.model_name = model_name
        self._device = _select_device()
        self.model = SentenceTransformer(model_name, device=self._device)
        self._query_prefix = _get_bge_query_prefix(model_name)
        # GPU 单次可处理更大 batch；CPU 保持默认 32
        _default_bs = "256" if "cuda" in self._device else "32"
        self._batch_size = int(os.getenv("EMBEDDING_BATCH_SIZE", _default_bs))
        print(f"[Embed] 模型={model_name}  设备={self._device}  batch_size={self._batch_size}")

    def embed(self, texts: List[str]) -> np.ndarray:
        """为文档文本列表生成向量（不添加查询前缀）。"""
        return self.model.encode(
            texts,
            batch_size=self._batch_size,
            show_progress_bar=len(texts) > 100,
            normalize_embeddings=True,
        )

    def embed_query(self, query: str) -> np.ndarray:
        """为查询生成向量（需要时自动添加 BGE 查询前缀）。"""
        prefixed = self._query_prefix + query if self._query_prefix else query
        return self.model.encode(
            [prefixed],
            batch_size=1,
            show_progress_bar=False,
            normalize_embeddings=True,
        )[0]


# Global embedding model
_embedding_model = None


def get_embedding_model() -> EmbeddingModel:
    """获取全局嵌入模型单例（不存在时自动创建）。"""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = EmbeddingModel()
    return _embedding_model
