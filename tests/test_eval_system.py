import unittest

from backend.schemas import AgentContextV1, PlanPlusV1, SessionStateV1
from core.metrics import add_event, trace_scope
from core.orchestration.runner import OrchestrationRunner
from core.runtime.executor import ExecutionRuntime
from core.services.rag_service import RAGService
from scripts.eval.dataset_lint import lint_cases
from scripts.eval.judge_runner import summarize_judge_rows
from scripts.eval.review_runner import build_review_report
from scripts.perf.bench_runner import _context_metrics, _taskgraph_status_coverage, _trace_contract


class EvalSystemTests(unittest.TestCase):
    def _plan(self) -> PlanPlusV1:
        return PlanPlusV1(
            need_rag=True,
            need_memory=True,
            allowed_tools=[],
            task_type="learn",
            resolved_mode="learn",
            style="step_by_step",
            output_format="answer",
            question_raw="解释矩阵秩",
            user_intent="学习讲解",
            retrieval_query="矩阵秩",
            memory_query="矩阵秩",
        )

    def _session_state(self) -> SessionStateV1:
        return SessionStateV1(
            session_id="sess-eval-1",
            course_name="course",
            requested_mode_hint="learn",
            resolved_mode="learn",
            task_full_text="解释矩阵秩",
            task_summary="解释矩阵秩",
        )

    def test_runtime_prefetch_threads_preserve_trace_context(self):
        runner = OrchestrationRunner()
        runtime = ExecutionRuntime(runner)
        runner.rag_service.retrieve = lambda **_kwargs: (
            add_event("retrieval", retrieval_ms=12.0, success=True) or "RAG", [], False
        )
        runner.memory_service.prefetch_history_ctx = lambda **_kwargs: (
            add_event("memory_prefetch", memory_prefetch_ms=4.0, success=True) or "MEM"
        )
        with trace_scope({"case_id": "trace_ctx"}) as trace:
            agent_context = runtime._build_agent_context(
                course_name="course",
                resolved_mode="learn",
                user_message="解释矩阵秩",
                history=[],
                plan=self._plan(),
                runtime_state={"session_state": self._session_state()},
            )
        self.assertIsInstance(agent_context, AgentContextV1)
        event_types = [event.get("type") for event in trace.events]
        self.assertIn("retrieval", event_types)
        self.assertIn("memory_prefetch", event_types)

    def test_rag_service_emits_missing_and_skipped_events(self):
        class _WorkspaceStore:
            @staticmethod
            def get_workspace_path(_course_name: str) -> str:
                return "tests/does_not_exist"

        service = RAGService(_WorkspaceStore())
        with trace_scope({"case_id": "rag_skip"}) as trace:
            service.retrieve(course_name="course", retrieval_query="q", mode="learn", need_rag=False)
            service.retrieve(course_name="course", retrieval_query="q", mode="learn", need_rag=True)
        event_types = [event.get("type") for event in trace.events]
        self.assertIn("retrieval_skipped", event_types)
        self.assertIn("retrieval_missing_index", event_types)

    def test_trace_contract_flags_missing_retrieval_trace(self):
        result = _trace_contract(
            case={"requires_citations": True},
            citations=[{"doc_id": "doc.pdf"}],
            retrieval_events=[],
            retrieval_missing_index_events=[],
            retrieval_skipped_events=[],
        )
        self.assertTrue(result["trace_contract_error"])
        self.assertEqual("citations_without_retrieval_trace", result["trace_contract_reason"])

    def test_context_metrics_and_taskgraph_coverage_helpers(self):
        metrics = _context_metrics(
            [
                {
                    "history_tokens_est": 100,
                    "rag_tokens_est": 200,
                    "memory_tokens_est": 50,
                    "final_tokens_est": 350,
                    "context_pressure_ratio": 0.7,
                }
            ]
        )
        self.assertEqual(100.0, metrics["avg_history_tokens_case"])
        self.assertEqual(350.0, metrics["avg_input_context_tokens_case"])
        coverage = _taskgraph_status_coverage(
            {
                "metadata": {
                    "taskgraph_statuses": {
                        "plan_intent": "completed",
                        "prefetch_rag": "completed",
                        "persist_session_state": "pending",
                    }
                }
            }
        )
        self.assertAlmostEqual(2.0 / 3.0, coverage)

    def test_dataset_lint_reports_schema_and_coverage(self):
        rows = [
            {
                "case_id": "a",
                "mode": "learn",
                "course_name": "矩阵理论",
                "message": "m1",
                "history": [{"role": "user", "content": "x"}],
                "expected_session_events": ["session_state_saved"],
                "tags": ["tool"],
            },
            {
                "case_id": "b",
                "mode": "practice",
                "course_name": "线性代数",
                "message": "m2",
                "history": [],
                "should_use_tools": True,
                "tags": ["fallback"],
            },
            {
                "case_id": "c",
                "mode": "exam",
                "course_name": "概率论",
                "message": "m3",
                "history": [{"role": "assistant", "content": "y"}],
                "expected_session_events": ["session_state_loaded"],
            },
            {
                "case_id": "d",
                "mode": "learn",
                "course_name": "信号与系统",
                "message": "m4",
                "history": [{"role": "assistant", "content": "z"}],
                "tags": ["route_override"],
            },
        ]
        report = lint_cases(
            rows,
            min_courses=4,
            min_multi_turn_ratio=0.25,
            min_session_ratio=0.15,
            min_tool_ratio=0.15,
            min_fallback_ratio=0.10,
        )
        self.assertTrue(report["ok"])

    def test_judge_summary_aggregates_scores(self):
        summary = summarize_judge_rows(
            [
                {
                    "case_id": "a",
                    "mode": "learn",
                    "judge_skipped": False,
                    "overall_score": 0.9,
                    "confidence": 0.8,
                    "label": "pass",
                    "pairwise_winner": "candidate",
                    "dimensions": {name: 0.9 for name in ("correctness", "groundedness", "completeness", "pedagogy_clarity", "instruction_following")},
                },
                {
                    "case_id": "b",
                    "mode": "practice",
                    "judge_skipped": False,
                    "overall_score": 0.5,
                    "confidence": 0.6,
                    "label": "fail",
                    "pairwise_winner": "baseline",
                    "dimensions": {name: 0.5 for name in ("correctness", "groundedness", "completeness", "pedagogy_clarity", "instruction_following")},
                },
            ]
        )
        self.assertEqual(2, summary["num_judged"])
        self.assertAlmostEqual(0.7, summary["avg_overall_score"])
        self.assertAlmostEqual(0.5, summary["pairwise_candidate_win_rate"])

    def test_review_report_marks_regressions_and_review_queue(self):
        report = build_review_report(
            benchmark_summary={"p50_e2e_latency_ms": 120.0},
            benchmark_rows=[
                {
                    "case_id": "learn_01",
                    "mode": "learn",
                    "e2e_latency_ms": 150.0,
                    "rag_hit": 0.0,
                    "fallback_rate_case": 1.0,
                    "trace_contract_error": True,
                    "latency_budget_met_case": 0.0,
                }
            ],
            judge_summary={"avg_overall_score": 0.5},
            judge_rows=[
                {
                    "case_id": "learn_01",
                    "mode": "learn",
                    "judge_skipped": False,
                    "overall_score": 0.5,
                    "confidence": 0.4,
                    "label": "fail",
                    "pairwise_winner": "baseline",
                }
            ],
            baseline_benchmark_summary={"p50_e2e_latency_ms": 100.0},
            baseline_benchmark_rows=[
                {"case_id": "learn_01", "mode": "learn", "e2e_latency_ms": 100.0, "rag_hit": 1.0}
            ],
            baseline_judge_summary={"avg_overall_score": 0.8},
            baseline_judge_rows=[
                {"case_id": "learn_01", "mode": "learn", "judge_skipped": False, "overall_score": 0.8}
            ],
        )
        self.assertEqual(1, report["headline"]["regression_case_count"])
        self.assertEqual(1, report["headline"]["human_review_queue_count"])
        reasons = report["regression_cases"][0]["reasons"]
        self.assertIn("trace_contract_error", reasons)
        self.assertIn("judge_score_regressed", reasons)


if __name__ == "__main__":
    unittest.main()
