"""Regression tests for v3 priority plan changes (P0/P1)."""

from __future__ import annotations

import os
import json
import tempfile
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from backend.schemas import SessionStateV1, ToolDecision
from core.errors import ToolDeniedError
from core.orchestration.context_budgeter import ContextBudgeter
from core.services.tool_hub import ToolHub
from core.services.workspace_store import WorkspaceStore
from core.services.shadow_eval_service import OnlineShadowEvalService
from mcp_tools.client import MCPTools
from memory.manager import MemoryManager
from memory.store import SQLiteMemoryStore


class V3PriorityPlanTests(unittest.TestCase):
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

    def test_workspace_store_session_cleanup_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
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

    def test_toolhub_group_gate_blocks_mismatched_group(self) -> None:
        hub = ToolHub()
        MCPTools._context = {
            "allowed_tool_groups": ["memory"],
            "tool_budget": {"per_request_total": 6, "per_round": 3, "per_tool": {"websearch": 2}},
            "tool_audit": [],
            "tool_usage": {"executed_total": 0, "per_tool": {}, "per_round": {}},
        }
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
        self.assertTrue(MCPTools._context.get("tool_audit"))
        audit_tail = MCPTools._context["tool_audit"][-1]
        self.assertEqual("tool_group_denied", audit_tail.get("reason"))
        self.assertIn("tool_budget_snapshot", audit_tail.get("metadata", {}))

    def test_memory_preferences_and_lru_like_eviction(self) -> None:
        td = tempfile.mkdtemp(prefix="cp_mem_")
        try:
            db_path = os.path.join(td, "memory.db")
            with mock.patch.dict(os.environ, {"MEMORY_DB_PATH": db_path}, clear=False):
                mgr = MemoryManager(user_id="u1")
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
            shutil.rmtree(td, ignore_errors=True)

    def test_online_shadow_eval_queue_and_process(self) -> None:
        with tempfile.TemporaryDirectory() as td:
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


if __name__ == "__main__":
    unittest.main()
