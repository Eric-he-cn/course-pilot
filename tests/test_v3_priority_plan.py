"""Regression tests for v3 priority plan changes (P0/P1)."""

from __future__ import annotations

import os
import json
import shutil
import uuid
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import backend.api as backend_api
from backend.schemas import RetrievedChunk, SessionStateV1, ToolDecision
from core.agents.router import RouterAgent
from core.errors import ToolDeniedError
from core.orchestration.context_budgeter import ContextBudgeter
from core.services.memory_service import MemoryService
from core.services.rag_service import RAGService
from core.services.tool_hub import ToolHub
from core.services.workspace_store import WorkspaceStore
from core.services.shadow_eval_service import OnlineShadowEvalService
from mcp_tools.client import MCPTools
from memory.manager import MemoryManager
from memory.store import SQLiteMemoryStore


class V3PriorityPlanTests(unittest.TestCase):
    @staticmethod
    def _local_tmpdir(prefix: str) -> str:
        base_dir = os.path.join(os.getcwd(), "tests", "_tmp")
        os.makedirs(base_dir, exist_ok=True)
        path = os.path.join(base_dir, f"{prefix}{uuid.uuid4().hex[:8]}")
        os.mkdir(path)
        return path

    def tearDown(self) -> None:
        MCPTools.clear_request_context()

    @staticmethod
    def _chunk(
        *,
        text: str = "教材片段",
        doc_id: str = "doc.pdf",
        page: int = 1,
        score: float = 0.03,
        dense_score: float | None = None,
        bm25_score: float | None = None,
        rrf_score: float | None = None,
    ) -> RetrievedChunk:
        return RetrievedChunk(
            text=text,
            doc_id=doc_id,
            page=page,
            score=score,
            dense_score=dense_score,
            bm25_score=bm25_score,
            rrf_score=rrf_score,
        )

    def test_context_budgeter_adaptive_compress_only_under_pressure(self) -> None:
        env = {
            "RAG_COMPRESSION_MODE": "adaptive",
            "RAG_COMPRESS_OWNER": "budgeter",
            "CTX_TOTAL_TOKENS": "600",
            "CTX_SAFETY_MARGIN": "100",
            "RAG_ADAPTIVE_PRESSURE_THRESHOLD": "0.6",
            "CB_HISTORY_RECENT_TURNS": "1",
            "CB_RECENT_RAW_TURNS": "1",
            "CB_ENABLE_LLM_HISTORY_COMPRESS": "0",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            budgeter = ContextBudgeter()
            with mock.patch.object(budgeter, "compress_rag_text", return_value="压缩后片段") as patched:
                high = budgeter.build_context(
                    query="解释注意力机制",
                    history=[],
                    rag_text="这是一个很长的教材片段。" * 180,
                    memory_text="",
                    rag_sent_per_chunk=2,
                    rag_sent_max_chars=120,
                )
                self.assertTrue(high["rag_adaptive_compress_applied"])
                self.assertGreaterEqual(patched.call_count, 1)

        env_low = dict(env)
        env_low["CTX_TOTAL_TOKENS"] = "12000"
        env_low["RAG_ADAPTIVE_PRESSURE_THRESHOLD"] = "0.95"
        with mock.patch.dict(os.environ, env_low, clear=False):
            budgeter = ContextBudgeter()
            with mock.patch.object(budgeter, "compress_rag_text", return_value="压缩后片段") as patched:
                low = budgeter.build_context(
                    query="解释注意力机制",
                    history=[],
                    rag_text="这是一个较短片段。",
                    memory_text="",
                    rag_sent_per_chunk=2,
                    rag_sent_max_chars=120,
                )
                self.assertFalse(low["rag_adaptive_compress_applied"])
                self.assertEqual(0, patched.call_count)

    def test_context_budgeter_skips_llm_history_compress_for_exam_mode(self) -> None:
        history = [{"role": "user", "content": "解释矩阵秩的定义和几何意义。"}] * 20
        env = {
            "CB_ENABLE_LLM_HISTORY_COMPRESS": "1",
            "CONTEXT_LLM_COMPRESSION_THRESHOLD": "0.5",
            "CB_DISABLE_LLM_HISTORY_COMPRESS_MODES": "practice,exam",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            budgeter = ContextBudgeter()
            with mock.patch.object(budgeter, "_llm_summary_card", return_value=("LLM摘要", 10.0, "llm")) as patched:
                packed = budgeter.build_context(
                    query="解释矩阵秩",
                    history=history,
                    rag_text="教材内容" * 300,
                    memory_text="",
                    rag_sent_per_chunk=2,
                    rag_sent_max_chars=120,
                    mode="exam",
                )
                self.assertEqual("heuristic", packed["history_summary_source"])
                self.assertEqual(0, patched.call_count)

    def test_workspace_store_session_cleanup_and_delete(self) -> None:
        td = self._local_tmpdir("workspace_store_")
        try:
            store = WorkspaceStore(td)
            course_name = "线性代数"
            old_state = SessionStateV1(
                session_id="sess-old",
                course_name=course_name,
                requested_mode_hint="learn",
                resolved_mode="learn",
                task_full_text="old",
                task_summary="old",
                metadata={"updated_at": (datetime.now() - timedelta(days=50)).isoformat()},
            )
            new_state = SessionStateV1(
                session_id="sess-new",
                course_name=course_name,
                requested_mode_hint="learn",
                resolved_mode="learn",
                task_full_text="new",
                task_summary="new",
            )
            store.save_session_state(old_state)
            store.save_session_state(new_state)
            # 覆盖旧会话的 updated_at，确保它会被 TTL 清理
            old_path = os.path.join(td, course_name, "sessions", "sess-old.json")
            with open(old_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            payload.setdefault("metadata", {})
            old_ts = (datetime.now() - timedelta(days=50)).timestamp()
            payload["metadata"]["updated_at"] = datetime.fromtimestamp(old_ts).isoformat()
            with open(old_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.utime(old_path, (old_ts, old_ts))

            summary = store.cleanup_session_states(course_name, ttl_days=30)
            self.assertIn("sess-old", summary["removed_session_ids"])
            self.assertIsNone(store.load_session_state(course_name, "sess-old"))
            self.assertIsNotNone(store.load_session_state(course_name, "sess-new"))
            self.assertTrue(store.delete_session_state(course_name, "sess-new"))
            self.assertFalse(store.delete_session_state(course_name, "sess-new"))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_toolhub_group_gate_blocks_mismatched_group(self) -> None:
        hub = ToolHub()
        MCPTools.set_request_context({
            "allowed_tool_groups": ["memory"],
            "tool_budget": {"per_request_total": 6, "per_round": 3, "per_tool": {"websearch": 2}},
            "tool_audit": [],
            "tool_usage": {"executed_total": 0, "per_tool": {}, "per_round": {}},
        })
        decision = ToolDecision(
            tool_name="websearch",
            allowed=True,
            reason="allowed",
            signature="websearch:query=test",
            permission_mode="standard",
            idempotency_key="idem-websearch",
        )
        with mock.patch.object(ToolHub, "decide", return_value=decision):
            with self.assertRaises(ToolDeniedError):
                hub.invoke(
                    tool_name="websearch",
                    tool_args={"query": "attention mechanism"},
                    mode="learn",
                    phase="act",
                    permission_mode="standard",
                    original_user_content="请帮我检索一下",
                    tool_cache={},
                    last_exec_ms={},
                    tool_retry_max=1,
                    tool_round=1,
                )
        self.assertTrue(MCPTools.get_request_context().tool_audit)
        audit_tail = MCPTools.get_request_context().tool_audit[-1]
        self.assertEqual("tool_group_denied", audit_tail.get("reason"))
        self.assertIn("tool_budget_snapshot", audit_tail.get("metadata", {}))
        MCPTools.clear_request_context()

    def test_memory_preferences_and_lru_like_eviction(self) -> None:
        td = self._local_tmpdir("cp_mem_")
        try:
            db_path = os.path.join(td, "memory.db")
            with mock.patch.dict(os.environ, {"MEMORY_DB_PATH": db_path}, clear=False):
                import memory.manager as memory_manager_module

                original_store = memory_manager_module._store
                memory_manager_module._store = None
                mgr = MemoryManager(user_id="u1")
                try:
                    mgr.upsert_preferences(
                        "线性代数",
                        [
                            {"text": "偏好中文回答", "source": "explicit"},
                            {"text": "偏好先讲思路再给答案", "source": "explicit"},
                        ],
                    )
                    mgr.upsert_preferences(
                        "线性代数",
                        [{"text": "偏好中文回答", "source": "implicit"}],
                        merge=True,
                    )
                    profile_ctx = mgr.get_profile_context("线性代数")
                    self.assertIn("偏好", profile_ctx)
                    stats = mgr.get_stats(course_name="线性代数")
                    self.assertTrue(isinstance(stats.get("preference_items"), list))

                    store = SQLiteMemoryStore(os.path.join(td, "evict.db"))
                    e1 = store.save_episode("线性代数", "qa", "old-low", importance=0.1, metadata={}, user_id="u1")
                    for i in range(50):
                        store.save_episode("线性代数", "qa", f"low-{i}", importance=0.1, metadata={}, user_id="u1")
                    e3 = store.save_episode("线性代数", "qa", "high", importance=0.95, metadata={}, user_id="u1")
                    store._touch_episode_ids([e3])
                    evicted = store.evict_episodes_soft_cap(
                        course_name="线性代数",
                        user_id="u1",
                        soft_cap=50,
                        batch_size=5,
                        protect_importance=0.8,
                    )
                    self.assertGreaterEqual(int(evicted.get("removed", 0)), 1)
                    remain = store.search_episodes(
                        "low high",
                        user_id="u1",
                        course_name="线性代数",
                        top_k=10,
                        min_importance=0.0,
                    )
                    remain_ids = {r.get("id") for r in remain}
                    self.assertIn(e3, remain_ids)
                    self.assertNotIn(e1, remain_ids)
                finally:
                    memory_manager_module._store = original_store
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_online_shadow_eval_queue_and_process(self) -> None:
        td = self._local_tmpdir("online_eval_")
        try:
            svc = OnlineShadowEvalService(base_dir=td)
            payload = {
                "case_id": "online_case_001",
                "course_name": "线性代数",
                "mode": "learn",
                "message": "解释矩阵秩",
                "history": [],
                "response_text": "矩阵秩是...",
                "citations": [],
                "e2e_latency_ms": 123.0,
                "first_token_latency_ms": 45.0,
            }
            svc.enqueue(payload)
            day_dir = Path(td) / datetime.now().strftime("%Y-%m-%d")
            state = {}
            with mock.patch.dict(os.environ, {"ONLINE_EVAL_RUN_JUDGE_REVIEW": "0"}, clear=False):
                svc._process_date_dir(day_dir, state)
            self.assertTrue((day_dir / "benchmark_raw_online.jsonl").exists())
            self.assertTrue((day_dir / "benchmark_summary_online.json").exists())
            self.assertTrue((day_dir / "cases_online.jsonl").exists())
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_memory_service_cache_is_request_scoped(self) -> None:
        service = MemoryService()
        mock_result = {"success": True, "results": [{"content": "历史错题 A"}]}
        with mock.patch("core.services.memory_service.MCPTools.call_tool", return_value=mock_result) as patched:
            MCPTools.set_request_context({"request_id": "req-1"})
            first = service.prefetch_history_ctx(query="复习之前的矩阵秩错题", course_name="线代", mode="learn")
            second = service.prefetch_history_ctx(query="复习之前的矩阵秩错题", course_name="线代", mode="learn")
            self.assertEqual(first, second)
            self.assertEqual(1, patched.call_count)

            MCPTools.clear_request_context()
            MCPTools.set_request_context({"request_id": "req-2"})
            service.prefetch_history_ctx(query="复习之前的矩阵秩错题", course_name="线代", mode="learn")
            self.assertEqual(2, patched.call_count)
        MCPTools.clear_request_context()

    def test_memory_prefetch_skips_without_history_intent(self) -> None:
        service = MemoryService()
        with mock.patch("core.services.memory_service.MCPTools.call_tool") as patched:
            ctx = service.prefetch_history_ctx(query="介绍矩阵秩的定义", course_name="线代", mode="learn")
        self.assertEqual("", ctx)
        self.assertEqual(0, patched.call_count)

    def test_memory_prefetch_relaxes_metadata_filters_after_empty_strict_match(self) -> None:
        service = MemoryService()
        strict_empty = {"success": True, "results": []}
        relaxed_hit = {"success": True, "results": [{"content": "历史错题：秩与线性无关关系混淆"}]}
        with mock.patch(
            "core.services.memory_service.MCPTools.call_tool",
            side_effect=[strict_empty, relaxed_hit],
        ) as patched:
            ctx = service.prefetch_history_ctx(
                query="复习之前矩阵秩相关错题",
                course_name="线代",
                mode="practice",
                agent="quizzer",
                phase="generate",
            )
        self.assertIn("历史错题", ctx)
        self.assertEqual(2, patched.call_count)

    def test_memory_prefetch_stops_after_hard_failure(self) -> None:
        service = MemoryService()
        failure = {"success": False, "error": "mock transport error", "failure_class": "fatal_error"}
        with mock.patch(
            "core.services.memory_service.MCPTools.call_tool",
            return_value=failure,
        ) as patched:
            ctx = service.prefetch_history_ctx(
                query="复习之前矩阵秩相关错题",
                course_name="线代",
                mode="practice",
                agent="quizzer",
                phase="generate",
            )
        self.assertEqual("", ctx)
        self.assertEqual(1, patched.call_count)

    def test_toolhub_memory_search_min_interval_branch_is_reachable(self) -> None:
        hub = ToolHub()
        MCPTools.set_request_context(
            {
                "tool_budget": {"per_request_total": 6, "per_round": 3},
                "tool_audit": [],
                "tool_usage": {"executed_total": 0, "per_tool": {}, "per_round": {}},
            }
        )
        decision = ToolDecision(
            tool_name="memory_search",
            allowed=True,
            reason="allowed",
            signature="memory_search:{\"course_name\":\"线代\",\"query\":\"矩阵秩\"}",
            permission_mode="safe",
            idempotency_key="idem-memory-search",
        )
        now_ms = 5000.0
        with mock.patch.object(ToolHub, "decide", return_value=decision), mock.patch(
            "core.services.tool_hub.perf_counter",
            return_value=now_ms / 1000.0,
        ):
            tool_cache = {}
            last_exec_ms = {decision.signature: now_ms - 100.0}
            dedup_decision, result = hub.invoke(
                tool_name="memory_search",
                tool_args={"query": "矩阵秩", "course_name": "线代"},
                mode="learn",
                phase="act",
                permission_mode="safe",
                original_user_content="之前矩阵秩讲过什么",
                tool_cache=tool_cache,
                last_exec_ms=last_exec_ms,
                tool_retry_max=0,
                tool_round=1,
            )
        self.assertTrue(dedup_decision.dedup_hit)
        self.assertEqual("memory_search_min_interval", dedup_decision.dedup_reason)
        self.assertTrue(result.get("success"))
        self.assertEqual([], result.get("results"))
        MCPTools.clear_request_context()

    def test_rag_service_cache_reuses_until_index_changes(self) -> None:
        td = self._local_tmpdir("rag_cache_")
        try:
            course_name = "线代"
            index_dir = Path(td) / course_name / "index"
            index_dir.mkdir(parents=True, exist_ok=True)
            faiss_path = index_dir / "faiss_index.faiss"
            pkl_path = index_dir / "faiss_index.pkl"
            faiss_path.write_bytes(b"faiss")
            pkl_path.write_bytes(b"meta")
            store = WorkspaceStore(td)

            with mock.patch("core.services.rag_service.FAISSStore") as store_cls, mock.patch(
                "core.services.rag_service.Retriever"
            ) as retriever_cls:
                retriever_cls.side_effect = lambda store_obj: {"store": store_obj}
                rag_service = RAGService(store)
                first = rag_service.load_retriever(course_name)
                second = rag_service.load_retriever(course_name)
                self.assertEqual(first, second)
                self.assertEqual(1, store_cls.return_value.load.call_count)

                next_mtime = faiss_path.stat().st_mtime + 5
                os.utime(faiss_path, (next_mtime, next_mtime))
                third = rag_service.load_retriever(course_name)
                self.assertNotEqual(id(first), id(third))
                self.assertEqual(2, store_cls.return_value.load.call_count)
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_rag_service_prefers_question_raw_before_rewrite_fallback(self) -> None:
        rag_service = RAGService(mock.Mock())

        class FakeRetriever:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def retrieve(self, query: str, **kwargs):
                self.calls.append((query, dict(kwargs)))
                return [
                    V3PriorityPlanTests._chunk(
                        text="Layer Norm 片段",
                        dense_score=0.41,
                        bm25_score=0.2,
                        rrf_score=0.03,
                        score=0.03,
                    )
                ]

            def format_context(self, chunks):
                return "CTX:" + "|".join(chunk.text for chunk in chunks)

        fake = FakeRetriever()
        with mock.patch.object(rag_service, "load_retriever", return_value=fake):
            ctx, citations, retrieval_empty = rag_service.retrieve(
                course_name="深度学习",
                question_raw="介绍一下Layer Norm",
                retrieval_query="解释 layer normalization 的核心原理",
                mode="learn",
                empty_message="EMPTY",
            )
        self.assertFalse(retrieval_empty)
        self.assertEqual("介绍一下Layer Norm", fake.calls[0][0])
        self.assertEqual(1, len(fake.calls))
        self.assertIn("Layer Norm", ctx)
        self.assertEqual(1, len(citations))
        self.assertTrue(citations[0].evidence_passed)
        self.assertAlmostEqual(0.41, float(citations[0].dense_score or 0.0), places=2)
        self.assertAlmostEqual(0.03, float(citations[0].rrf_score or 0.0), places=2)
        self.assertAlmostEqual(float(citations[0].score), float(citations[0].rrf_score or 0.0), places=4)

    def test_rag_service_falls_back_to_rewrite_when_primary_has_no_evidence(self) -> None:
        rag_service = RAGService(mock.Mock())

        class FakeRetriever:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def retrieve(self, query: str, **kwargs):
                self.calls.append((query, dict(kwargs)))
                if len(self.calls) == 1:
                    return [
                        V3PriorityPlanTests._chunk(
                            text="弱相关片段",
                            dense_score=0.39,
                            bm25_score=0.8,
                            rrf_score=0.03,
                            score=0.03,
                        )
                    ]
                return [
                    V3PriorityPlanTests._chunk(
                        text="rewrite 命中片段",
                        dense_score=0.42,
                        bm25_score=0.9,
                        rrf_score=0.02,
                        score=0.02,
                    )
                ]

            def format_context(self, chunks):
                return "CTX:" + "|".join(chunk.text for chunk in chunks)

        fake = FakeRetriever()
        with mock.patch.object(rag_service, "load_retriever", return_value=fake):
            ctx, citations, retrieval_empty = rag_service.retrieve(
                course_name="深度学习",
                question_raw="介绍一下Layer Norm",
                retrieval_query="解释 layer normalization 的定义与作用",
                mode="learn",
                empty_message="EMPTY",
            )
        self.assertFalse(retrieval_empty)
        self.assertEqual(2, len(fake.calls))
        self.assertEqual("介绍一下Layer Norm", fake.calls[0][0])
        self.assertEqual("解释 layer normalization 的定义与作用", fake.calls[1][0])
        self.assertTrue(bool(fake.calls[1][1].get("rewrite_fallback_triggered")))
        self.assertIn("rewrite 命中片段", ctx)
        self.assertEqual("rewrite 命中片段", citations[0].text)
        self.assertTrue(citations[0].evidence_passed)

    def test_rag_service_rejects_low_evidence_chunks(self) -> None:
        rag_service = RAGService(mock.Mock())

        class FakeRetriever:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def retrieve(self, query: str, **kwargs):
                self.calls.append((query, dict(kwargs)))
                return [
                    V3PriorityPlanTests._chunk(
                        text="泛 Transformer 片段",
                        dense_score=0.31,
                        bm25_score=0.4,
                        rrf_score=0.03,
                        score=0.03,
                    )
                ]

            def format_context(self, chunks):
                return "CTX:" + "|".join(chunk.text for chunk in chunks)

        fake = FakeRetriever()
        with mock.patch.object(rag_service, "load_retriever", return_value=fake):
            ctx, citations, retrieval_empty = rag_service.retrieve(
                course_name="深度学习",
                question_raw="介绍一下Layer Norm",
                retrieval_query="介绍一下Layer Norm",
                mode="learn",
                empty_message="EMPTY",
            )
        self.assertTrue(retrieval_empty)
        self.assertEqual("EMPTY", ctx)
        self.assertEqual([], citations)

    def test_rag_service_evidence_gate_respects_rank_limit(self) -> None:
        rag_service = RAGService(mock.Mock())
        chunks = [
            self._chunk(text=f"片段{i}", dense_score=0.2, bm25_score=0.2, rrf_score=0.03 - i * 0.001, score=0.03 - i * 0.001)
            for i in range(4)
        ]
        chunks.append(self._chunk(text="高分但排名靠后", dense_score=0.6, bm25_score=2.0, rrf_score=0.01, score=0.01))
        gated = rag_service._gate_chunks(chunks)
        self.assertEqual([], gated)

    def test_sqlite_store_enables_wal_and_busy_timeout(self) -> None:
        td = self._local_tmpdir("sqlite_pragmas_")
        try:
            store = SQLiteMemoryStore(os.path.join(td, "memory.db"))
            with store._conn() as conn:
                journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
            self.assertEqual("wal", str(journal_mode).lower())
            self.assertEqual(5000, int(busy_timeout))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_router_strict_schema_path_parses_and_normalizes(self) -> None:
        router = RouterAgent()
        session_state = SessionStateV1(
            session_id="sess-router-1",
            course_name="深度学习",
            requested_mode_hint="learn",
            resolved_mode="learn",
            task_full_text="介绍一下 Layer Norm",
            task_summary="介绍一下 Layer Norm",
        )
        payload = {
            "need_rag": True,
            "style": "step_by_step",
            "output_format": "answer",
            "question_raw": "介绍一下 Layer Norm",
            "user_intent": "解释 Layer Norm",
            "retrieval_keywords": ["Layer Norm"],
            "retrieval_query": "介绍一下 Layer Norm",
            "memory_query": "介绍一下 Layer Norm",
            "workflow_template": "learn_only",
            "action_kind": "learn_explain",
            "route_confidence": 0.9,
            "route_reason": "学习型概念解释",
            "required_artifact_kind": "none",
            "tool_budget": {"per_request_total": 6, "per_round": 3},
            "allowed_tool_groups": ["teaching", "rag"],
        }
        with mock.patch.dict(
            os.environ,
            {"ENABLE_STRUCTURED_OUTPUTS_ROUTER": "1", "ROUTER_PLAN_RETRY_ON_PARSE_FAIL": "1"},
            clear=False,
        ), mock.patch.object(router, "_structured_chat_json", return_value=payload), mock.patch.object(
            router, "invoke_llm", side_effect=AssertionError("plain JSON fallback should not run")
        ):
            plan = router.plan("介绍一下 Layer Norm", "learn", "深度学习", session_state=session_state)
        self.assertEqual("learn_only", plan.workflow_template)
        self.assertEqual("learn", plan.resolved_mode)
        self.assertEqual("介绍一下 Layer Norm", plan.question_raw)

    def test_router_retries_once_before_default_fallback(self) -> None:
        router = RouterAgent()
        session_state = SessionStateV1(
            session_id="sess-router-2",
            course_name="深度学习",
            requested_mode_hint="learn",
            resolved_mode="learn",
            task_full_text="介绍一下 Layer Norm",
            task_summary="介绍一下 Layer Norm",
        )
        retry_payload = {
            "need_rag": True,
            "style": "step_by_step",
            "output_format": "answer",
            "question_raw": "介绍一下 Layer Norm",
            "user_intent": "解释 Layer Norm",
            "retrieval_keywords": ["Layer Norm"],
            "retrieval_query": "介绍一下 Layer Norm",
            "memory_query": "介绍一下 Layer Norm",
            "workflow_template": "learn_only",
            "action_kind": "learn_explain",
            "route_confidence": 0.88,
            "route_reason": "retry fixed",
            "required_artifact_kind": "none",
            "tool_budget": {"per_request_total": 6, "per_round": 3},
            "allowed_tool_groups": ["teaching", "rag"],
        }
        with mock.patch.dict(
            os.environ,
            {"ENABLE_STRUCTURED_OUTPUTS_ROUTER": "1", "ROUTER_PLAN_RETRY_ON_PARSE_FAIL": "1"},
            clear=False,
        ), mock.patch.object(
            router, "_structured_chat_json", side_effect=[{}, retry_payload]
        ), mock.patch.object(
            router, "invoke_llm", side_effect=ValueError("invalid_json_payload")
        ):
            plan = router.plan("介绍一下 Layer Norm", "learn", "深度学习", session_state=session_state)
        self.assertEqual("retry fixed", plan.route_reason)
        self.assertEqual("learn_only", plan.workflow_template)

    def test_router_falls_back_to_default_after_retry_failure(self) -> None:
        router = RouterAgent()
        session_state = SessionStateV1(
            session_id="sess-router-3",
            course_name="深度学习",
            requested_mode_hint="practice",
            resolved_mode="practice",
            task_full_text="帮我出一道题",
            task_summary="帮我出一道题",
        )
        with mock.patch.dict(
            os.environ,
            {"ENABLE_STRUCTURED_OUTPUTS_ROUTER": "1", "ROUTER_PLAN_RETRY_ON_PARSE_FAIL": "1"},
            clear=False,
        ), mock.patch.object(router, "_structured_chat_json", return_value={}), mock.patch.object(
            router, "invoke_llm", side_effect=ValueError("invalid_json_payload")
        ):
            plan = router.plan("帮我出一道题", "practice", "深度学习", session_state=session_state)
        self.assertEqual("practice", plan.resolved_mode)
        self.assertEqual("practice_only", plan.workflow_template)

    def test_embedding_preload_runs_on_startup_when_enabled(self) -> None:
        with mock.patch.dict(os.environ, {"EMBEDDING_PRELOAD_ON_STARTUP": "1"}, clear=False), mock.patch(
            "rag.embed.get_embedding_model", return_value=object()
        ) as patched:
            import asyncio

            asyncio.run(backend_api._preload_embedding_model())
        self.assertEqual(1, patched.call_count)


if __name__ == "__main__":
    unittest.main()
