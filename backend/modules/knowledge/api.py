from __future__ import annotations


class KnowledgeFeatureDisabledError(ValueError):
    """Returned when a Wiki build is requested before its course flag is enabled."""


class MaterialNotIndexedError(ValueError):
    """A Wiki build requires already searchable material; it must not imply indexing."""


class WikiBuildInProgressError(ValueError):
    """这门课的知识页正在构建。构建会把整页重写一遍，此时保存手写区必然丢更新。"""


class WikiPageTooLargeError(ValueError):
    """手写区把整页撑过了大小上限。整页被拒绝，盘上的内容原样留着。"""
