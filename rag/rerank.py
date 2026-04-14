"""
【模块说明】
- 主要作用：封装 Cross-Encoder reranker 的加载与打分。
- 核心类：RerankerModel。
- 核心函数：get_reranker_model（全局单例获取）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import threading
from typing import List

import numpy as np
import torch
from sentence_transformers import CrossEncoder


def _select_device() -> str:
    """自动选择 reranker 设备。"""
    env = os.getenv("RERANK_DEVICE", "auto").strip().lower()
    if env != "auto":
        return env
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        print(f"[Rerank] 使用 GPU: {gpu_name}")
        return "cuda"
    print("[Rerank] 未检测到 CUDA，使用 CPU")
    return "cpu"


class RerankerModel:
    """Cross-Encoder reranker 封装。"""

    def __init__(self, model_name: str | None = None):
        if model_name is None:
            model_name = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")
        self.model_name = model_name
        self._device = _select_device()
        self.model = CrossEncoder(model_name, device=self._device)
        default_bs = "32" if "cuda" in self._device else "8"
        self._batch_size = max(1, int(os.getenv("RERANK_BATCH_SIZE", default_bs)))
        print(f"[Rerank] 模型={model_name}  设备={self._device}  batch_size={self._batch_size}")

    def rerank(self, query: str, texts: List[str]) -> List[float]:
        """对 query + candidate texts 打分，返回与 texts 对齐的分数列表。"""
        clean_texts = [str(text or "") for text in texts]
        if not clean_texts:
            return []
        pairs = [(query, text) for text in clean_texts]
        scores = self.model.predict(
            pairs,
            batch_size=self._batch_size,
            show_progress_bar=False,
        )
        if isinstance(scores, np.ndarray):
            return [float(x) for x in scores.tolist()]
        if isinstance(scores, list):
            return [float(x) for x in scores]
        return [float(scores)]


_reranker_model: RerankerModel | None = None
_reranker_lock = threading.Lock()


def get_reranker_model() -> RerankerModel:
    """获取全局 reranker 单例（模型名变更时自动重建）。"""
    global _reranker_model
    model_name = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base").strip() or "BAAI/bge-reranker-base"
    with _reranker_lock:
        if _reranker_model is None:
            _reranker_model = RerankerModel(model_name=model_name)
        elif str(getattr(_reranker_model, "model_name", "") or "") != model_name:
            _reranker_model = RerankerModel(model_name=model_name)
        return _reranker_model
