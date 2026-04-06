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

from backend.schemas import Plan
from core.agents.quizmaster import QuizMasterAgent
from core.agents.router import RouterAgent
from core.orchestration.runner import OrchestrationRunner
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
            self.assertEqual(2, qa_count)
            self.assertEqual(1, summary_count)
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
            while time.time() < deadline:
                rows = store.get_recent_episodes("course", limit=20)
                if any(r.get("event_type") == "qa_summary" for r in rows):
                    break
                time.sleep(0.05)
            summary_count = sum(1 for row in rows if row.get("event_type") == "qa_summary")
            qa_count = sum(1 for row in rows if row.get("event_type") == "qa")
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
