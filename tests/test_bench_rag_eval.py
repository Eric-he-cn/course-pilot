"""Benchmark RAG evaluation contract tests."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.perf.bench_runner import (  # noqa: E402
    _evaluate_rag_against_gold,
    _gold_coverage,
    _recompute_rows_with_gold,
)


class BenchRagEvalTests(unittest.TestCase):
    def test_doc_page_match_strategy(self) -> None:
        citations = [
            {
                "doc_id": "矩阵理论教案.pdf",
                "page": 206,
                "chunk_id": "矩阵理论教案.pdf_p206_c0",
                "text": "矩阵的最大秩分解...",
            }
        ]
        gold = {
            "gold_doc_ids": ["矩阵理论教案.pdf"],
            "gold_pages": [206],
            "should_retrieve": True,
        }

        result = _evaluate_rag_against_gold(citations=citations, response_text="", gold_target=gold)
        self.assertEqual("doc_page", result["rag_match_strategy"])
        self.assertEqual(1.0, result["rag_hit"])
        self.assertEqual(1.0, result["rag_top1"])

    def test_keyword_match_strategy(self) -> None:
        citations = [
            {
                "doc_id": "未知材料.pdf",
                "page": 12,
                "text": "该题讨论了线性相关与矩阵秩的关系",
            }
        ]
        gold = {
            "gold_keywords": ["线性相关", "矩阵秩"],
            "should_retrieve": True,
        }

        result = _evaluate_rag_against_gold(citations=citations, response_text="", gold_target=gold)
        self.assertEqual("keyword", result["rag_match_strategy"])
        self.assertEqual(1.0, result["rag_hit"])

    def test_gold_coverage_by_case_id(self) -> None:
        cases = [{"case_id": "a"}, {"case_id": "b"}, {"case_id": "c"}]
        gold = {"a": {}, "c": {}}
        stats = _gold_coverage(cases, gold)
        self.assertEqual(3.0, stats["case_total"])
        self.assertEqual(2.0, stats["gold_matched"])
        self.assertAlmostEqual(2.0 / 3.0, stats["gold_coverage"])

    def test_recompute_rows_updates_rag_fields(self) -> None:
        rows = [
            {
                "case_id": "learn_01",
                "response_text": "解释矩阵秩",
                "citations": [
                    {
                        "doc_id": "矩阵理论教案.pdf",
                        "page": 206,
                        "chunk_id": "矩阵理论教案.pdf_p206_c0",
                        "text": "矩阵秩定义",
                    }
                ],
                "rag_hit": 0.0,
                "rag_has_gold": 0.0,
            }
        ]
        gold_targets = {
            "learn_01": {
                "gold_doc_ids": ["矩阵理论教案.pdf"],
                "gold_pages": [206],
                "gold_chunk_ids": [],
                "gold_keywords": [],
                "should_retrieve": True,
            }
        }

        out = _recompute_rows_with_gold(rows, gold_targets)
        self.assertEqual(1, len(out))
        self.assertEqual(1.0, out[0]["rag_has_gold"])
        self.assertEqual(1.0, out[0]["rag_hit"])
        self.assertEqual("doc_page", out[0]["rag_match_strategy"])


if __name__ == "__main__":
    unittest.main()
