"""
【模块说明】
- 主要作用：提供轻量级 BM25 词法检索能力，作为向量检索的补充。
- 核心类：BM25Index。
- 设计要点：不依赖第三方 BM25 库，默认支持中英文混合分词。
"""
import math
import re
from collections import Counter
from typing import Any, Dict, List, Tuple


def _tokenize(text: str) -> List[str]:
    """中英文混合分词：英文/数字按词切，中文按单字切。"""
    if not text:
        return []
    normalized = text.lower()
    # 英文词、数字串、中文单字
    return re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", normalized)


class BM25Index:
    """基于文本块列表构建的 BM25 检索索引。"""

    def __init__(self, chunks: List[Dict[str, Any]], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks or []
        self.k1 = k1
        self.b = b
        self._doc_tfs: List[Dict[str, int]] = []
        self._doc_lens: List[int] = []
        self._idf: Dict[str, float] = {}
        self._avgdl = 0.0
        self._build()

    def _build(self) -> None:
        """预计算每个文档的 TF 与全局 IDF。"""
        n_docs = len(self.chunks)
        if n_docs == 0:
            return

        doc_freq: Counter = Counter()

        for chunk in self.chunks:
            tokens = _tokenize(chunk.get("text", ""))
            tf = Counter(tokens)
            self._doc_tfs.append(dict(tf))
            self._doc_lens.append(len(tokens))
            for term in tf.keys():
                doc_freq[term] += 1

        self._avgdl = (sum(self._doc_lens) / n_docs) if n_docs else 0.0
        for term, df in doc_freq.items():
            # 标准 BM25 常用平滑 idf 形式
            self._idf[term] = math.log1p((n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 3) -> List[Tuple[Dict[str, Any], float]]:
        """按 BM25 评分返回 top_k 文本块。"""
        if not self.chunks:
            return []

        top_k = max(1, int(top_k))
        query_terms = Counter(_tokenize(query))
        if not query_terms:
            return []

        avgdl = self._avgdl if self._avgdl > 0 else 1.0
        scores: List[float] = [0.0] * len(self.chunks)

        for i, tf_map in enumerate(self._doc_tfs):
            dl = self._doc_lens[i] if self._doc_lens[i] > 0 else 1
            doc_score = 0.0
            for term, qtf in query_terms.items():
                tf = tf_map.get(term, 0)
                if tf <= 0:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + self.k1 * (1.0 - self.b + self.b * (dl / avgdl))
                doc_score += idf * ((tf * (self.k1 + 1.0)) / denom) * qtf
            scores[i] = doc_score

        ranked = sorted(
            ((idx, score) for idx, score in enumerate(scores) if score > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        return [(self.chunks[idx], float(score)) for idx, score in ranked[:top_k]]

