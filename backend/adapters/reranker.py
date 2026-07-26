from __future__ import annotations

import threading

# 阈值怎么标定出来的（默认值那张表在 core/settings.py 的 CALIBRATED_RERANK_THRESHOLDS）：
# 候选取 dense top-20，同一批问题分别打到「操作系统」和「深度学习」两个库上，对一个库是
# 正例、对另一个就是负例，各 13 条，标签逐条核对过原文。
#
# v2-m3 的分布很干净：13 个负例里 12 个 ≤ 0.060、最高 0.182；正例除一个孤点 0.0507 外
# 全部 ≥ 0.735。所以 0.2~0.7 之间取哪个值结果都一样，取 0.3 是让两侧都留出余量。
# 那个孤点是问法敏感（「衡量调度好坏用什么指标」换成「调度指标是什么」就到 0.65），
# 检索工具本来就会提示模型换个说法再查一次，这条路径能兜住它。
#
# bge-reranker-base 跨语言不可靠：同一批 chunk，中文问法 0.0095、英文问法 0.9977。
# 这个项目的核心场景就是中文提问打英文教材，所以它不做默认。

# query 侧的长度闸门。CrossEncoder 的截断策略是 longest_first——砍长的那一侧，所以
# 一条很长的 query（比如贴一整道题干）会把文档那半截掉。v2-m3 的窗口是 8192 token，
# 这个上限配 600 字符的 chunk 有很大余量；换成 512 窗口的模型要把它调到 200 以内。
_QUERY_CHAR_LIMIT = 2000


class CrossEncoderReranker:
    """Sentence-Transformers CrossEncoder 适配器：懒加载，加载失败后保持不可用不再重试。"""

    def __init__(self, *, model_name: str, device: str = "auto", batch_size: int = 16) -> None:
        self._model_name = model_name
        self._device = device
        self._batch_size = max(1, batch_size)
        self._load_lock = threading.Lock()
        self._model = None
        self._error: str | None = None

    @property
    def name(self) -> str:
        return self._model_name

    def status(self) -> dict[str, object]:
        return {"model": self._model_name, "loaded": self._model is not None, "error": self._error}

    def _load(self):
        with self._load_lock:
            if self._model is None and self._error is None:
                try:
                    from sentence_transformers import CrossEncoder
                    from torch.nn import Sigmoid

                    device = None if self._device in {"", "auto"} else self._device
                    # 显式指定激活函数。默认值是 ST 按 num_labels=1 推出来的，模型自己的
                    # config 能改写它——真被改成 Identity 的话，输出就变成无界 logit，
                    # 阈值会静默失去意义。
                    self._model = CrossEncoder(self._model_name, device=device, activation_fn=Sigmoid())
                except Exception as error:
                    self._error = f"{type(error).__name__}: {error}"
            return self._model

    def rerank(self, *, query: str, documents: list[str]) -> list[float] | None:
        model = self._load()
        if model is None or not documents:
            return None
        try:
            # 推理不加锁：reranker 只在查询路径上，没有索引 worker 那种长时间占用模型的
            # 竞争者，实测并发调用结果逐位一致。
            scores = model.predict(
                [(query[:_QUERY_CHAR_LIMIT], document) for document in documents],
                batch_size=self._batch_size, show_progress_bar=False,
            )
            return [float(score) for score in scores]
        except Exception as error:
            # 一次推理失败不该打挂整次检索；标记不可用后退回 RRF 排序。
            self._error = f"{type(error).__name__}: {error}"
            return None
