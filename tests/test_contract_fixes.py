"""
契约回归测试（第二轮审阅修复项）。

说明：
- 该文件使用 unittest，便于在无 pytest 时直接运行。
- 不触发真实联网调用，仅做本地链路契约校验。
"""

import inspect
import json
import os
import shutil
import time
import unittest
from unittest import mock

from backend.schemas import AgentContextV1, ChatMessage, Plan, PlanPlusV1, SessionStateV1
from core.agents.quizmaster import QuizMasterAgent
from core.agents.router import RouterAgent
from core.orchestration.runner import OrchestrationRunner
from core.runtime.executor import ExecutionRuntime
from core.services.event_bus import EventBus
from core.services.tool_hub import ToolHub
from mcp_tools.client import MCPTools
from memory.manager import MemoryManager
from memory.store import SQLiteMemoryStore


class _FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, temperature=0.0, max_tokens=0, response_format=None):
        self.calls.append(
            {
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        if not self.responses:
            raise RuntimeError("no_more_fake_responses")
        return self.responses.pop(0)


class ContractFixTests(unittest.TestCase):
    @staticmethod
    def _plan(task_type: str) -> Plan:
        return Plan(
            need_rag=False,
            allowed_tools=[],
            task_type=task_type,
            style="step_by_step",
            output_format="answer",
        )

    @staticmethod
    def _stream_has_context_budget(gen, max_steps=30) -> bool:
        try:
            for _ in range(max_steps):
                chunk = next(gen)
                if isinstance(chunk, dict) and "__context_budget__" in chunk:
                    return True
        except StopIteration:
            return False
        finally:
            try:
                gen.close()
            except Exception:
                pass
        return False

    def test_non_stream_mode_funcs_are_not_generators(self):
        self.assertFalse(inspect.isgeneratorfunction(OrchestrationRunner.run_learn_mode))
        self.assertFalse(inspect.isgeneratorfunction(OrchestrationRunner.run_practice_mode))
        self.assertFalse(inspect.isgeneratorfunction(OrchestrationRunner.run_exam_mode))

    def test_stream_modes_emit_context_budget_event(self):
        runner = OrchestrationRunner()
        runner.load_retriever = lambda _course: None
        runner._fetch_history_ctx = lambda **_kwargs: ""
        runner.tutor.teach_stream = lambda *_args, **_kwargs: iter(["ok"])

        # 练习/考试流程中，出题分支需要 QuizMaster 返回结构化对象；这里给最小桩。
        from backend.schemas import Quiz

        runner.quizmaster.generate_quiz = lambda **_kwargs: Quiz(
            question="1. 示例选择题\nA. 选项A\nB. 选项B",
            standard_answer="A",
            rubric="按标准答案判分",
            difficulty="medium",
            chapter="测试章节",
            concept="测试知识点",
        )
        runner.quizmaster.generate_exam_paper = lambda **_kwargs: {
            "content": "# 模拟考试试卷\n\n1. 示例题（100分）",
            "answer_sheet": [{"id": 1, "type": "简答题", "score": 100, "standard_answer": "示例"}],
            "total_score": 100,
        }

        learn_gen = runner.run_learn_mode_stream("course", "hello", self._plan("learn"), history=[])
        practice_gen = runner.run_practice_mode_stream("course", "出一道题", self._plan("practice"), history=[])
        exam_gen = runner.run_exam_mode_stream("course", "出一套卷子", self._plan("exam"), history=[])

        self.assertTrue(self._stream_has_context_budget(learn_gen))
        self.assertTrue(self._stream_has_context_budget(practice_gen))
        self.assertTrue(self._stream_has_context_budget(exam_gen))

    def test_quiz_question_type_locked_and_mcq_retry(self):
        os.environ["ENABLE_STRUCTURED_OUTPUTS_QUIZ"] = "0"
        qm = QuizMasterAgent()

        qm._plan_quiz = lambda **_kwargs: {
            "topic": "前馈神经网络",
            "num_questions": 1,
            "difficulty": "medium",
            "question_type": "简答题",  # 故意与请求冲突，验证锁题型
            "focus_points": [],
        }
        qm._build_external_ctx = lambda _query: ""

        invalid_mcq = json.dumps(
            {
                "question": "请解释前馈神经网络的定义。",
                "standard_answer": "前馈神经网络是...",
                "rubric": "答到定义即可",
                "difficulty": "medium",
                "chapter": "神经网络",
                "concept": "FFN",
            },
            ensure_ascii=False,
        )
        valid_mcq = json.dumps(
            {
                "question": "前馈神经网络的信息流方向是？\nA. 输入到输出\nB. 输出到输入\nC. 双向循环\nD. 任意方向",
                "standard_answer": "A",
                "rubric": "选 A 得分",
                "difficulty": "medium",
                "chapter": "神经网络",
                "concept": "FFN",
            },
            ensure_ascii=False,
        )
        fake_llm = _FakeLLM([invalid_mcq, valid_mcq])
        qm.llm = fake_llm

        quiz = qm.generate_quiz(
            course_name="course",
            topic="FFN",
            difficulty="medium",
            context="",
            question_type="选择题",
        )

        self.assertEqual(len(fake_llm.calls), 2, "选择题形态不合格时应触发单次重试")
        first_prompt = fake_llm.calls[0]["messages"][1]["content"]
        self.assertIn("题型: 选择题", first_prompt, "用户指定题型应被锁定为选择题")
        self.assertIn("A.", quiz.question)
        self.assertEqual(quiz.standard_answer.upper(), "A")

    def test_exam_total_score_normalized_to_100(self):
        exam_json = {
            "title": "测试卷",
            "instructions": "测试说明",
            "questions": [
                {"type": "选择题", "question": "Q1", "options": ["A.1", "B.2"], "score": 10, "standard_answer": "A", "rubric": "", "chapter": "1", "concept": "c1"},
                {"type": "判断题", "question": "Q2", "options": ["A.对", "B.错"], "score": 20, "standard_answer": "A", "rubric": "", "chapter": "1", "concept": "c2"},
                {"type": "简答题", "question": "Q3", "options": [], "score": 30, "standard_answer": "x", "rubric": "", "chapter": "2", "concept": "c3"},
            ],
        }
        paper = QuizMasterAgent._render_exam_paper("course", exam_json)
        answer_sheet = paper.get("answer_sheet", [])
        total = sum(int(x.get("score", 0) or 0) for x in answer_sheet)
        self.assertEqual(100, total)
        self.assertEqual(100, int(paper.get("total_score", 0) or 0))

    def test_exam_generation_fail_closed_on_empty_questions(self):
        os.environ["ENABLE_STRUCTURED_OUTPUTS_EXAM"] = "0"
        qm = QuizMasterAgent()
        qm._plan_exam = lambda **_kwargs: {
            "scope": "Attention",
            "num_questions": 2,
            "difficulty_ratio": {"easy": 1, "medium": 1, "hard": 0},
        }
        qm._build_external_ctx = lambda _query: ""
        qm.llm = _FakeLLM(['{"title":"测试卷","instructions":"说明","questions":[]}'])

        payload = qm.generate_exam_paper(
            course_name="course",
            user_request="出一套卷子",
            context="",
        )
        self.assertTrue(payload.get("_artifact_error"))
        self.assertEqual([], payload.get("answer_sheet", []))
        self.assertIn("试卷生成失败", payload.get("content", ""))

    def test_quiz_fallback_does_not_expose_raw_json(self):
        os.environ["ENABLE_STRUCTURED_OUTPUTS_QUIZ"] = "0"
        qm = QuizMasterAgent()
        qm._plan_quiz = lambda **_kwargs: {
            "topic": "Attention",
            "num_questions": 1,
            "difficulty": "medium",
            "question_type": "选择题",
            "focus_points": [],
        }
        qm._build_external_ctx = lambda _query: ""
        qm.llm = _FakeLLM(['{"foo":"bar"}'])

        quiz = qm.generate_quiz(
            course_name="course",
            topic="Attention",
            difficulty="medium",
            context="",
            question_type="选择题",
        )
        self.assertNotIn('"foo"', quiz.question)
        self.assertNotIn("bar", quiz.question)
        self.assertIn("A.", quiz.question)

    def test_practice_multi_question_routes_to_exam_payload(self):
        runner = OrchestrationRunner()
        runner._fetch_history_ctx = lambda **_kwargs: ""
        runner._save_practice_record = lambda *_args, **_kwargs: "practices/mock.md"
        runner._save_grading_to_memory = lambda *_args, **_kwargs: None
        runner.quizmaster.generate_quiz = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("generate_quiz_should_not_be_called_for_multi")
        )
        runner.quizmaster.generate_exam_paper = lambda **_kwargs: {
            "content": "# 模拟考试试卷\n\n1. A/B/C/D 选择题（10分）",
            "answer_sheet": [{"id": 1, "type": "选择题", "score": 100, "standard_answer": "A"}],
            "total_score": 100,
        }
        runner._resolve_quiz_request = lambda _msg: ("Attention", "medium", 10, "选择题")

        resp = runner.run_practice_mode(
            course_name="course",
            user_message="出10道Attention相关的选择题",
            plan=self._plan("practice"),
            history=[],
        )
        self.assertIn("练习题（多题）", resp.content)
        self.assertTrue(resp.tool_calls and isinstance(resp.tool_calls, list))
        self.assertEqual("exam_meta", resp.tool_calls[0].get("name"))

    def test_practice_answer_submission_uses_exam_grader_when_exam_meta_exists(self):
        runner = OrchestrationRunner()
        runner._fetch_history_ctx = lambda **_kwargs: ""
        runner._save_practice_record = lambda *_args, **_kwargs: "practices/mock.md"
        runner._save_grading_to_memory = lambda *_args, **_kwargs: None
        runner.grader.grade_practice_stream = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("grade_practice_stream_should_not_be_called_when_exam_meta_exists")
        )
        runner.grader.grade_exam_stream = lambda **_kwargs: iter(["评分完成：90/100"])

        history = [
            {
                "role": "assistant",
                "content": "# 练习题（多题）\n\n1. ...",
                "tool_calls": [
                    {
                        "type": "internal_meta",
                        "name": "exam_meta",
                        "payload": {
                            "answer_sheet": [{"id": 1, "type": "选择题", "score": 100, "standard_answer": "A"}],
                            "total_score": 100,
                        },
                    }
                ],
            }
        ]
        resp = runner.run_practice_mode(
            course_name="course",
            user_message="A",
            plan=self._plan("practice"),
            history=history,
        )
        self.assertIn("评分完成：90/100", resp.content)

    def test_router_normalize_plan_fills_rewrite_fields(self):
        plan = RouterAgent._normalize_plan(
            {
                "need_rag": True,
                "style": "step_by_step",
                "output_format": "answer",
            },
            "learn",
            "解释一下 attention 的作用",
        )
        self.assertEqual("解释一下 attention 的作用", plan.question_raw)
        self.assertEqual("解释一下 attention 的作用", plan.retrieval_query)
        self.assertEqual("解释一下 attention 的作用", plan.memory_query)
        self.assertTrue(isinstance(plan.retrieval_keywords, list) and len(plan.retrieval_keywords) > 0)

    def test_router_can_resolve_mode_from_user_intent(self):
        plan = RouterAgent._normalize_plan(
            {
                "need_rag": True,
                "style": "step_by_step",
                "output_format": "exam",
            },
            "learn",
            "请帮我出一套模拟考试试卷",
        )
        self.assertEqual("exam", plan.resolved_mode)
        self.assertEqual("exam", plan.task_type)
        self.assertIn("调整", plan.mode_reason)

    def test_agent_build_context_returns_agent_context_v1(self):
        session_state = SessionStateV1(
            session_id="sess-agent-ctx",
            course_name="course",
            requested_mode_hint="learn",
            resolved_mode="learn",
            task_full_text="解释 Attention",
            task_summary="解释 Attention",
        )
        ctx = RouterAgent().build_context(
            session_state,
            course_name="course",
            user_message="解释 Attention",
            mode_hint="learn",
        )
        self.assertIsInstance(ctx, AgentContextV1)
        self.assertEqual("sess-agent-ctx", ctx.session_snapshot.session_id)
        self.assertIn("mode_hint", ctx.constraints)

    def test_learn_mode_emits_session_state_meta(self):
        from backend.schemas import TutorResult

        runner = OrchestrationRunner()
        runner.load_retriever = lambda _course: None
        runner._fetch_history_ctx = lambda **_kwargs: ""
        runner.tutor.teach = lambda *_args, **_kwargs: TutorResult(content="好的，这里是讲解。")

        resp = runner.run_learn_mode(
            course_name="course",
            user_message="解释一下 Attention",
            plan=self._plan("learn"),
            history=[],
            state={"session_id": "sess-learn-1"},
        )

        session_payload = None
        for tool_call in resp.tool_calls or []:
            if tool_call.get("name") == "session_state":
                session_payload = tool_call.get("payload")
                break
        self.assertIsNotNone(session_payload)
        self.assertEqual("sess-learn-1", session_payload["session_id"])
        self.assertEqual("learn_completed", session_payload["current_stage"])

    def test_runtime_compiles_taskgraph_for_practice_multi_question(self):
        runner = OrchestrationRunner()
        runtime = ExecutionRuntime(runner)
        session_state = SessionStateV1(
            session_id="sess-graph-1",
            course_name="course",
            requested_mode_hint="practice",
            resolved_mode="practice",
            task_full_text="出10道Attention相关的选择题",
            task_summary="出10道Attention相关的选择题",
        )
        plan = PlanPlusV1(
            need_rag=True,
            need_memory=True,
            allowed_tools=[],
            task_type="practice",
            resolved_mode="practice",
            style="step_by_step",
            output_format="answer",
            question_raw="出10道Attention相关的选择题",
            user_intent="练习出题",
            retrieval_query="Attention 选择题",
            memory_query="Attention 选择题",
            capabilities=["rag", "memory"],
            workflow_template="practice_only",
            action_kind="practice_generate",
        )

        graph = runtime.compile_taskgraph(
            course_name="course",
            mode_hint="practice",
            user_message="出10道Attention相关的选择题",
            history=[],
            plan=plan,
            session_state=session_state,
            stream=False,
        )

        self.assertEqual("run_exam", graph.route)
        self.assertEqual("practice_only", graph.workflow_template)
        self.assertIn("run_exam", graph.step_names())
        self.assertIn("persist_session_state", graph.step_names())
        self.assertTrue(graph.metadata.get("digest"))

    def test_runtime_compiles_composite_learn_then_practice_template(self):
        runner = OrchestrationRunner()
        runtime = ExecutionRuntime(runner)
        session_state = SessionStateV1(
            session_id="sess-graph-composite-1",
            course_name="course",
            requested_mode_hint="learn",
            resolved_mode="learn",
            task_full_text="先讲解再出一道题",
            task_summary="先讲解再出一道题",
        )
        plan = PlanPlusV1(
            need_rag=True,
            need_memory=True,
            allowed_tools=[],
            task_type="learn",
            resolved_mode="learn",
            style="step_by_step",
            output_format="answer",
            question_raw="先讲解再出一道题",
            user_intent="先讲解再出题",
            retrieval_query="Attention 讲解",
            memory_query="Attention 讲解",
            workflow_template="learn_then_practice",
            action_kind="learn_then_practice",
        )
        graph = runtime.compile_taskgraph(
            course_name="course",
            mode_hint="learn",
            user_message="先讲解 Attention，再出一道练习题",
            history=[],
            plan=plan,
            session_state=session_state,
            stream=False,
        )
        self.assertEqual("learn_then_practice", graph.workflow_template)
        self.assertIn("run_tutor", graph.step_names())
        self.assertTrue(any(step in graph.step_names() for step in ("run_quiz", "run_exam")))
        self.assertEqual(2, len(graph.metadata.get("execute_plan", [])))

    def test_router_normalizes_general_like_input_to_supported_template(self):
        plan = RouterAgent._normalize_plan(
            {
                "need_rag": True,
                "style": "step_by_step",
                "output_format": "answer",
            },
            "general",
            "先解释一下 Attention，再出一道练习题。",
        )
        self.assertEqual("learn_then_practice", plan.workflow_template)
        self.assertEqual("learn", plan.resolved_mode)

    def test_session_state_restores_active_practice_from_visible_history(self):
        runner = OrchestrationRunner()
        history = [
            {
                "role": "assistant",
                "content": "## 练习题\n\n说明矩阵秩的定义，并给出一个 2x2 矩阵的秩。\n\n请回答上述题目，回答完毕后我会为你评分并给出详细讲解。",
            }
        ]
        restored = runner._extract_session_state(
            history=history,
            course_name="course",
            mode_hint="practice",
            user_message="我的答案是：矩阵秩就是非零行的个数。",
            state={},
        )
        self.assertTrue(restored.active_practice)
        self.assertEqual("practice", restored.active_practice["kind"])

    def test_session_state_restores_active_exam_from_visible_history(self):
        runner = OrchestrationRunner()
        history = [
            {
                "role": "assistant",
                "content": "# 《矩阵理论》模拟考试试卷\n\n一、解释矩阵秩的定义。\n二、说明矩阵可逆的判定条件。\n三、解释特征值的含义。\n\n请将各题答案统一整理后一次性提交。",
            }
        ]
        restored = runner._extract_session_state(
            history=history,
            course_name="course",
            mode_hint="exam",
            user_message="这是我的答卷：第一题我写了秩的定义。",
            state={},
        )
        self.assertTrue(restored.active_exam)
        self.assertEqual("exam", restored.active_exam["kind"])

    def test_router_prefers_review_template_when_active_artifact_and_answer_like(self):
        session_state = SessionStateV1(
            session_id="sess-review-route",
            course_name="course",
            requested_mode_hint="practice",
            resolved_mode="practice",
            task_full_text="练习评分",
            task_summary="练习评分",
            active_practice={"kind": "practice", "questions": [{"id": 1, "question": "Q"}]},
        )
        plan = RouterAgent._normalize_plan(
            {
                "need_rag": True,
                "style": "step_by_step",
                "output_format": "answer",
                "workflow_template": "practice_only",
            },
            "practice",
            "我的答案是：矩阵秩就是非零行的个数。",
            session_state,
        )
        self.assertEqual("practice_then_review", plan.workflow_template)
        self.assertEqual("practice_grade", plan.action_kind)

    def test_runtime_compiles_persist_memory_for_explicit_learn_memory_request(self):
        runner = OrchestrationRunner()
        runtime = ExecutionRuntime(runner)
        session_state = SessionStateV1(
            session_id="sess-graph-learn-1",
            course_name="course",
            requested_mode_hint="learn",
            resolved_mode="learn",
            task_full_text="请记住我以后喜欢先讲直觉再讲公式",
            task_summary="请记住我以后喜欢先讲直觉再讲公式",
        )
        plan = PlanPlusV1(
            need_rag=False,
            need_memory=True,
            allowed_tools=[],
            task_type="learn",
            resolved_mode="learn",
            style="step_by_step",
            output_format="answer",
            question_raw="请记住我以后喜欢先讲直觉再讲公式",
            user_intent="记住学习偏好",
            retrieval_query="学习偏好",
            memory_query="学习偏好",
        )
        graph = runtime.compile_taskgraph(
            course_name="course",
            mode_hint="learn",
            user_message="请记住我以后喜欢先讲直觉再讲公式",
            history=[],
            plan=plan,
            session_state=session_state,
            stream=False,
        )
        self.assertIn("persist_memory", graph.step_names())

    def test_session_state_restores_from_workspace_json(self):
        runner = OrchestrationRunner()
        course_name = "course_restore_case"
        session_id = "sess-restore-1"
        workspace_path = runner.get_workspace_path(course_name)
        shutil.rmtree(workspace_path, ignore_errors=True)
        session_state = SessionStateV1(
            session_id=session_id,
            course_name=course_name,
            requested_mode_hint="practice",
            resolved_mode="practice",
            task_full_text="旧任务",
            task_summary="旧任务",
            current_stage="practice_generated",
        )
        runner.workspace_store.save_session_state(session_state)
        restored = runner._extract_session_state(
            history=[],
            course_name=course_name,
            mode_hint="practice",
            user_message="继续这个会话",
            state={"session_id": session_id},
        )
        self.assertEqual(session_id, restored.session_id)
        self.assertEqual("practice_generated", restored.current_stage)
        self.assertEqual("继续这个会话", restored.task_full_text)
        shutil.rmtree(workspace_path, ignore_errors=True)

    def test_strict_new_runtime_raises_without_fallback(self):
        runner = OrchestrationRunner()
        old_strict = os.getenv("STRICT_NEW_RUNTIME")
        old_replan = os.getenv("ENABLE_ROUTER_REPLAN")
        os.environ["STRICT_NEW_RUNTIME"] = "1"
        os.environ["ENABLE_ROUTER_REPLAN"] = "0"
        try:
            runner.router.plan = lambda *_args, **_kwargs: PlanPlusV1(
                need_rag=False,
                need_memory=False,
                allowed_tools=[],
                task_type="learn",
                resolved_mode="learn",
                style="step_by_step",
                output_format="answer",
                question_raw="解释 Attention",
                user_intent="学习讲解",
                retrieval_query="Attention",
                memory_query="Attention",
            )
            runner.runtime._build_prefetch_bundle = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
            with self.assertRaises(RuntimeError):
                runner.run(
                    course_name="course",
                    mode="learn",
                    user_message="解释 Attention",
                    state={"session_id": "sess-strict-1"},
                    history=[],
                )
        finally:
            if old_strict is None:
                os.environ.pop("STRICT_NEW_RUNTIME", None)
            else:
                os.environ["STRICT_NEW_RUNTIME"] = old_strict
            if old_replan is None:
                os.environ.pop("ENABLE_ROUTER_REPLAN", None)
            else:
                os.environ["ENABLE_ROUTER_REPLAN"] = old_replan

    def test_stream_fallback_emits_hidden_tool_calls_only_once(self):
        from backend.schemas import TutorResult

        runner = OrchestrationRunner()
        old_replan = os.getenv("ENABLE_ROUTER_REPLAN")
        os.environ["ENABLE_ROUTER_REPLAN"] = "0"
        try:
            runner.router.plan = lambda *_args, **_kwargs: PlanPlusV1(
                need_rag=False,
                need_memory=False,
                allowed_tools=[],
                task_type="learn",
                resolved_mode="learn",
                style="step_by_step",
                output_format="answer",
                question_raw="解释 Attention",
                user_intent="学习讲解",
                retrieval_query="Attention",
                memory_query="Attention",
            )
            runner.runtime._build_prefetch_bundle = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
            runner.tutor.teach_stream = lambda *_args, **_kwargs: iter(["ok"])

            tool_call_events = []
            chunks = list(
                runner.run_stream(
                    course_name="course",
                    mode="learn",
                    user_message="解释 Attention",
                    state={"session_id": "sess-fallback-stream-1"},
                    history=[],
                )
            )
            for chunk in chunks:
                if isinstance(chunk, dict) and "__tool_calls__" in chunk:
                    tool_call_events.append(chunk)
            self.assertEqual(1, len(tool_call_events))
        finally:
            if old_replan is None:
                os.environ.pop("ENABLE_ROUTER_REPLAN", None)
            else:
                os.environ["ENABLE_ROUTER_REPLAN"] = old_replan

    def test_tool_hub_permission_and_idempotency(self):
        hub = ToolHub()
        original_ctx = dict(MCPTools._context)
        MCPTools._context = {"session_id": "sess-tool-1", "taskgraph_step": "run_tutor"}
        with mock.patch("core.services.tool_hub.MCPTools.call_tool", return_value={"success": True, "result": 4, "via": "mcp_stdio"}):
            with self.assertRaises(Exception):
                hub.invoke(
                    tool_name="filewriter",
                    tool_args={"filename": "a.md", "content": "x"},
                    mode="learn",
                    phase="act",
                    permission_mode="standard",
                    original_user_content="写笔记",
                    tool_cache={},
                    last_exec_ms={},
                    tool_retry_max=0,
                    tool_round=1,
                )
            decision, result = hub.invoke(
                tool_name="calculator",
                tool_args={"expression": "2+2"},
                mode="learn",
                phase="act",
                permission_mode="safe",
                original_user_content="算一下",
                tool_cache={},
                last_exec_ms={},
                tool_retry_max=0,
                tool_round=1,
            )
            self.assertTrue(decision.allowed)
            self.assertTrue(decision.idempotency_key.startswith("calculator:"))
            self.assertTrue(result["success"])
        MCPTools._context = original_ctx

    def test_tool_hub_enforces_total_and_per_tool_caps(self):
        hub = ToolHub()
        original_ctx = dict(MCPTools._context)
        MCPTools._context = {
            "session_id": "sess-cap-1",
            "taskgraph_step": "run_tutor",
            "tool_budget": {"per_request_total": 1, "per_round": 1, "calculator": 1},
        }
        try:
            with mock.patch("core.services.tool_hub.MCPTools.call_tool", return_value={"success": True, "result": 4, "via": "mcp_stdio"}):
                hub.invoke(
                    tool_name="calculator",
                    tool_args={"expression": "2+2"},
                    mode="learn",
                    phase="act",
                    permission_mode="safe",
                    original_user_content="算一下",
                    tool_cache={},
                    last_exec_ms={},
                    tool_retry_max=0,
                    tool_round=1,
                )
                with self.assertRaises(Exception):
                    hub.invoke(
                        tool_name="calculator",
                        tool_args={"expression": "3+3"},
                        mode="learn",
                        phase="act",
                        permission_mode="safe",
                        original_user_content="再算一下",
                        tool_cache={},
                        last_exec_ms={},
                        tool_retry_max=0,
                        tool_round=1,
                    )
        finally:
            MCPTools._context = original_ctx

    def test_event_bus_hidden_event_shapes_are_compatible(self):
        bus = EventBus()
        self.assertEqual({"__status__": "x"}, bus.status("x"))
        self.assertEqual({"__citations__": [{"doc_id": "d"}]}, bus.citations([{"doc_id": "d"}]))
        self.assertEqual({"__context_budget__": {"mode": "learn"}}, bus.context_budget({"mode": "learn"}))
        self.assertEqual({"__tool_calls__": [{"name": "session_state"}]}, bus.tool_calls([{"name": "session_state"}]))

    def test_runner_run_uses_runtime_resolved_mode(self):
        runner = OrchestrationRunner()
        old_replan = os.getenv("ENABLE_ROUTER_REPLAN")
        os.environ["ENABLE_ROUTER_REPLAN"] = "0"
        try:
            runner.router.plan = lambda *_args, **_kwargs: PlanPlusV1(
                need_rag=False,
                need_memory=True,
                allowed_tools=[],
                task_type="exam",
                resolved_mode="exam",
                style="step_by_step",
                output_format="answer",
                question_raw="请出一套模拟考试试卷",
                user_intent="考试出卷",
                retrieval_query="模拟考试试卷",
                memory_query="模拟考试试卷",
                capabilities=["memory"],
            )
            runner.run_learn_mode = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("learn_should_not_run")
            )
            runner.run_practice_mode = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("practice_should_not_run")
            )
            runner.run_exam_mode = lambda *_args, **_kwargs: ChatMessage(
                role="assistant",
                content="exam route selected",
                citations=None,
                tool_calls=None,
            )

            response, plan = runner.run(
                course_name="course",
                mode="learn",
                user_message="请出一套模拟考试试卷",
                state={"session_id": "sess-runtime-1"},
                history=[],
            )
            self.assertEqual("exam", plan.resolved_mode)
            self.assertEqual("exam route selected", response.content)
        finally:
            if old_replan is None:
                os.environ.pop("ENABLE_ROUTER_REPLAN", None)
            else:
                os.environ["ENABLE_ROUTER_REPLAN"] = old_replan

    def test_history_recent_trim_defaults_to_five_turns(self):
        history = []
        for i in range(14):
            role = "user" if i % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"msg-{i}"})
        trimmed = OrchestrationRunner._trim_history_recent(history)
        self.assertEqual(10, len(trimmed))
        self.assertEqual("msg-4", trimmed[0]["content"])
        self.assertEqual("msg-13", trimmed[-1]["content"])

    def test_learn_memory_persist_only_on_explicit_request(self):
        self.assertFalse(OrchestrationRunner._should_persist_learn_episode("解释一下 Transformer"))
        self.assertTrue(OrchestrationRunner._should_persist_learn_episode("请记住我以后喜欢先讲直觉再讲公式"))

    def test_qa_archive_compacts_old_records_into_summary(self):
        tmpdir = os.path.join(os.getcwd(), "tests", "_tmp_memory_case")
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        os.makedirs(tmpdir, exist_ok=True)
        try:
            db_path = os.path.join(tmpdir, "memory.db")
            store = SQLiteMemoryStore(db_path=db_path)
            for idx in range(4):
                store.save_episode(
                    course_name="course",
                    event_type="qa",
                    content=f"问题: 第{idx}次提问关于Attention",
                    importance=0.5,
                    metadata={"idx": idx},
                )

            result = store.compact_old_qa("course", retain_recent=1, batch_size=2)
            self.assertTrue(result["created"])
            all_rows = store.get_recent_episodes("course", limit=10)
            qa_count = sum(1 for row in all_rows if row.get("event_type") == "qa")
            summary_count = sum(1 for row in all_rows if row.get("event_type") == "qa_summary")
            self.assertLessEqual(qa_count, 2)
            self.assertEqual(1, summary_count)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_memory_search_touches_last_accessed_at(self):
        tmpdir = os.path.join(os.getcwd(), "tests", "_tmp_memory_case_touch")
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        os.makedirs(tmpdir, exist_ok=True)
        try:
            db_path = os.path.join(tmpdir, "memory.db")
            store = SQLiteMemoryStore(db_path=db_path)
            episode_id = store.save_episode(
                course_name="course",
                event_type="qa",
                content="问题: Attention 的作用是什么",
                importance=0.5,
                metadata={"idx": 1},
            )
            with store._conn() as conn:
                before = conn.execute(
                    "SELECT created_at, last_accessed_at FROM episodes WHERE id=?",
                    (episode_id,),
                ).fetchone()
            time.sleep(0.02)
            results = store.search_episodes("Attention", "course", top_k=1)
            self.assertEqual(episode_id, results[0]["id"])
            with store._conn() as conn:
                after = conn.execute(
                    "SELECT created_at, last_accessed_at FROM episodes WHERE id=?",
                    (episode_id,),
                ).fetchone()
            self.assertEqual(before["created_at"], before["last_accessed_at"])
            self.assertGreater(after["last_accessed_at"], before["last_accessed_at"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _history_turns(count: int, tool_calls: list = None):
        history = []
        for idx in range(1, count + 1):
            history.append({"role": "user", "content": f"user-{idx}"})
            assistant_msg = {"role": "assistant", "content": f"assistant-{idx}"}
            if idx == count and tool_calls is not None:
                assistant_msg["tool_calls"] = tool_calls
            history.append(assistant_msg)
        return history

    def test_rolling_history_summary_generates_every_five_turns(self):
        runner = OrchestrationRunner()
        calls = []

        def _fake_compress(turns):
            calls.append(len(turns))
            return {
                "summary_text": f"summary-{len(calls)}",
                "source": "llm",
                "tokens_est": 32,
                "elapsed_ms": 7.5,
            }

        runner.context_budgeter.compress_history_block = _fake_compress

        history10 = self._history_turns(10)
        state1, pending1, recent1, metrics1 = runner._prepare_history_summary_inputs(history10)
        self.assertEqual([5], calls)
        self.assertEqual(1, len(state1["blocks"]))
        self.assertEqual("summary-1", state1["blocks"][0]["summary_text"])
        self.assertEqual(5, state1["covered_turns"])
        self.assertEqual(0, len(pending1))
        self.assertEqual(10, len(recent1))
        self.assertEqual(1, metrics1["history_summary_block_count"])

        history15 = self._history_turns(
            15,
            tool_calls=runner._build_history_summary_tool_call(state1),
        )
        state2, pending2, recent2, metrics2 = runner._prepare_history_summary_inputs(history15)
        self.assertEqual([5, 5], calls, "新增 5 轮时应只压缩一个新 block")
        self.assertEqual(2, len(state2["blocks"]))
        self.assertEqual("summary-1", state2["blocks"][0]["summary_text"])
        self.assertEqual("summary-2", state2["blocks"][1]["summary_text"])
        self.assertEqual(10, state2["covered_turns"])
        self.assertEqual(0, len(pending2))
        self.assertEqual(10, len(recent2))
        self.assertEqual(2, metrics2["history_summary_block_count"])

    def test_rolling_history_summary_caps_at_ten_blocks(self):
        runner = OrchestrationRunner()
        runner.context_budgeter.compress_history_block = lambda turns: {
            "summary_text": f"summary-{turns[0][0]['content']}",
            "source": "llm",
            "tokens_est": 24,
            "elapsed_ms": 5.0,
        }
        history60 = self._history_turns(60)
        state, pending, recent, _metrics = runner._prepare_history_summary_inputs(history60)
        self.assertEqual(10, len(state["blocks"]))
        self.assertEqual(55, state["covered_turns"])
        self.assertEqual(0, len(pending))
        self.assertEqual(10, len(recent))

    def test_memory_manager_archives_old_qa_with_summary(self):
        tmpdir = os.path.join(os.getcwd(), "tests", "_tmp_memory_case_manager")
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        os.makedirs(tmpdir, exist_ok=True)
        old_env = {
            "MEMORY_QA_ARCHIVE_ENABLE": os.getenv("MEMORY_QA_ARCHIVE_ENABLE"),
            "MEMORY_QA_RETAIN_RECENT": os.getenv("MEMORY_QA_RETAIN_RECENT"),
            "MEMORY_QA_ARCHIVE_BATCH": os.getenv("MEMORY_QA_ARCHIVE_BATCH"),
        }
        try:
            os.environ["MEMORY_QA_ARCHIVE_ENABLE"] = "1"
            os.environ["MEMORY_QA_RETAIN_RECENT"] = "2"
            os.environ["MEMORY_QA_ARCHIVE_BATCH"] = "2"
            db_path = os.path.join(tmpdir, "memory.db")
            store = SQLiteMemoryStore(db_path=db_path)
            mgr = MemoryManager(store=store)
            mgr._summarize_qa_rows = lambda rows: (
                "历史学习问答摘要：\n- Attention\n- Transformer",
                {
                    "source": "test",
                    "source_ids": [r["id"] for r in rows],
                    "source_count": len(rows),
                },
            )
            for idx in range(5):
                mgr.record_event(
                    course_name="course",
                    event_type="qa",
                    content=f"问题: 第{idx}次提问关于Attention",
                    importance=0.3,
                    metadata={"idx": idx},
                )
            deadline = time.time() + 2.0
            rows = []
            qa_count = 999
            summary_count = 0
            while time.time() < deadline:
                rows = store.get_recent_episodes("course", limit=20)
                summary_count = sum(1 for row in rows if row.get("event_type") == "qa_summary")
                qa_count = sum(1 for row in rows if row.get("event_type") == "qa")
                if summary_count >= 1 and qa_count < 5:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(summary_count, 1)
            self.assertLess(qa_count, 5)
        finally:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
