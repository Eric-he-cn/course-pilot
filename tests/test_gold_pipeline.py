import tempfile
import unittest
from pathlib import Path

from scripts.eval.dataset_lint import _iter_case_files
from scripts.eval.gold_pipeline_utils import (
    build_official_gold_row,
    build_case_id,
    derive_gold_keywords,
    generate_question_suggestions,
    load_processed_case_ids,
    write_jsonl,
    scan_indexed_courses,
)
from scripts.eval.gold_screen_judge import normalize_judge_result


class GoldPipelineTests(unittest.TestCase):
    def test_scan_indexed_courses_filters_and_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for course_name in ["矩阵理论", "深度学习", "course", "perf_demo", "通信网络"]:
                index_dir = root / course_name / "index"
                index_dir.mkdir(parents=True, exist_ok=True)
                if course_name not in {"course"}:
                    (index_dir / "faiss_index.faiss").write_text("", encoding="utf-8")
                    (index_dir / "faiss_index.pkl").write_text("", encoding="utf-8")
            courses = scan_indexed_courses(root)
            explicit_courses = scan_indexed_courses(root, included_courses=["矩阵理论", "通信网络"])
        self.assertEqual(["深度学习", "通信网络"], courses)
        self.assertEqual(["矩阵理论", "通信网络"], explicit_courses)

    def test_generate_question_suggestions_returns_requested_count(self):
        courses = ["矩阵理论", "深度学习", "通信网络", "新中特", "LLM基础"]
        rows = generate_question_suggestions(courses, total=30)
        self.assertEqual(30, len(rows))
        self.assertEqual("矩阵理论", rows[0]["course_name"])

    def test_build_case_id_is_stable_for_same_question(self):
        left = build_case_id("矩阵理论", "解释矩阵的秩")
        right = build_case_id("矩阵理论", "解释矩阵的秩")
        other = build_case_id("矩阵理论", "解释特征值")
        self.assertEqual(left, right)
        self.assertNotEqual(left, other)

    def test_normalize_judge_result_requires_selected_citations_for_candidate(self):
        payload = {"response_text": "回答", "citations": [{"doc_id": "a.pdf"}], "trace_summary": {"fallback": False, "retrieval_empty": False}}
        result = normalize_judge_result(
            payload=payload,
            raw_result={
                "decision": "candidate",
                "confidence": 0.91,
                "reasoning": "looks_good",
                "selected_citation_indexes": [],
                "citation_quality_score": 0.92,
                "answer_grounded_score": 0.94,
                "coverage_score": 0.90,
            },
            threshold=0.85,
        )
        self.assertEqual("manual_fix", result["decision"])

    def test_normalize_judge_result_computes_overall_from_dimensions(self):
        payload = {"response_text": "回答", "citations": [{"doc_id": "a.pdf"}, {"doc_id": "b.pdf"}], "trace_summary": {"fallback": False, "retrieval_empty": False}}
        result = normalize_judge_result(
            payload=payload,
            raw_result={
                "decision": "candidate",
                "confidence": 0.90,
                "reasoning": "ok",
                "selected_citation_indexes": [0],
                "citation_quality_score": 0.80,
                "answer_grounded_score": 0.90,
                "coverage_score": 0.70,
            },
            threshold=0.85,
        )
        self.assertEqual(0.83, result["overall_score"])
        self.assertEqual("manual_fix", result["decision"])

    def test_normalize_judge_result_rejects_weak_evidence(self):
        payload = {"response_text": "回答", "citations": [{"doc_id": "a.pdf"}], "trace_summary": {"fallback": False, "retrieval_empty": False}}
        result = normalize_judge_result(
            payload=payload,
            raw_result={
                "decision": "candidate",
                "confidence": 0.90,
                "reasoning": "weak",
                "selected_citation_indexes": [],
                "citation_quality_score": 0.20,
                "answer_grounded_score": 0.30,
                "coverage_score": 0.20,
            },
            threshold=0.85,
        )
        self.assertEqual("reject", result["decision"])

    def test_build_official_gold_row_only_uses_selected_real_citations(self):
        candidate = {
            "case_id": "human_matrix_1",
            "message": "解释矩阵的秩",
            "plan_summary": {"retrieval_query": "矩阵的秩"},
            "citations": [
                {"doc_id": "矩阵理论教案.pdf", "page": 10, "chunk_id": "a"},
                {"doc_id": "矩阵理论教案.pdf", "page": 12, "chunk_id": "b"},
            ],
        }
        row = build_official_gold_row(candidate, selected_indexes=[1])
        self.assertEqual(["矩阵理论教案.pdf"], row["gold_doc_ids"])
        self.assertEqual([12], row["gold_pages"])
        self.assertEqual(["b"], row["gold_chunk_ids"])
        self.assertIn("解释矩阵的秩", row["gold_keywords"])

    def test_iter_case_files_skips_pipeline_transient_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gold_candidates.jsonl").write_text("{}", encoding="utf-8")
            (root / "gold_manual_fix.jsonl").write_text("{}", encoding="utf-8")
            (root / "cases_v1.jsonl").write_text("{}", encoding="utf-8")
            (root / "custom_cases.jsonl").write_text("{}", encoding="utf-8")
            files = [path.name for path in _iter_case_files(root)]
        self.assertEqual(["custom_cases.jsonl"], files)

    def test_load_processed_case_ids_reads_all_pipeline_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(root / "gold_candidates.jsonl", [{"case_id": "a"}])
            write_jsonl(root / "gold_manual_fix.jsonl", [{"case_id": "b"}])
            write_jsonl(root / "gold_rejected.jsonl", [{"case_id": "c"}])
            write_jsonl(root / "cases_v1.jsonl", [{"case_id": "d"}])
            processed = load_processed_case_ids(root)
        self.assertEqual({"a", "b", "c", "d"}, processed)

    def test_derive_gold_keywords_is_stable(self):
        keywords = derive_gold_keywords("请解释矩阵的秩与线性无关性的关系", "矩阵的秩 线性无关性")
        self.assertGreaterEqual(len(keywords), 2)


if __name__ == "__main__":
    unittest.main()
