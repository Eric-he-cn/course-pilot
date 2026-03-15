"""
【模块说明】
- 主要作用：实现系统主编排器，统一调度 Router/Tutor/Grader、RAG、MCP 工具与记忆系统。
- 核心类：OrchestrationRunner。
- 核心流程：run/run_stream（总入口）+ 各模式执行（learn/practice/exam）。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
"""
import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime
from core.metrics import get_active_trace

from backend.schemas import (
    Plan, ChatMessage, RetrievedChunk, Quiz, GradeReport,
    TutorResult, PracticeGradeSignal
)
from core.agents.router import RouterAgent
from core.agents.tutor import TutorAgent
from core.agents.quizmaster import QuizMasterAgent
from core.agents.grader import GraderAgent
from core.orchestration.context_budgeter import ContextBudgeter
from rag.retrieve import Retriever
from rag.store_faiss import FAISSStore
from mcp_tools.client import MCPTools
# 说明：练习/考试的出题与评分已拆分到 QuizMaster/Grader，Runner 只负责编排与持久化。


class OrchestrationRunner:
    """课程学习系统主编排器。"""
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR", "./data/workspaces")
        self.data_dir = data_dir
        
        # 初始化各 Agent
        self.router = RouterAgent()
        self.tutor = TutorAgent()
        self.quizmaster = QuizMasterAgent()
        self.grader = GraderAgent()
        self.tools = MCPTools()
        self.context_budgeter = ContextBudgeter()
        # 保存本轮执行元信息，供 Replan 判定使用。
        self._last_run_meta: Dict[str, Any] = {}
        self.logger = logging.getLogger("runner")

    @staticmethod
    def _trace_tag() -> str:
        trace = get_active_trace()
        if trace is None:
            return ""
        request_id = str((trace.meta or {}).get("request_id", "")).strip() or "unknown"
        return f" request_id={request_id} trace_id={trace.trace_id}"

    """执行质量量化：长度、句子数和异常信号综合判断。"""
    @staticmethod
    def _quality_metrics(content: str) -> Dict[str, Any]:
        import re

        text = (content or "").strip()
        min_chars = int(os.getenv("REPLAN_MIN_CHARS", "160"))
        min_sentences = int(os.getenv("REPLAN_MIN_SENTENCES", "2"))
        sentence_count = len(re.findall(r"[。！？!?\.]", text))
        bad_signals = [
            "Error calling LLM",
            "工具调用失败",
            "请重试",
            "（工具调用出错",
            "MCP tools/call failed",
        ]
        has_bad_signal = any(sig in text for sig in bad_signals)
        too_short = len(text) < min_chars
        too_few_sentences = sentence_count < min_sentences
        low_quality = has_bad_signal or too_short or too_few_sentences
        return {
            "low_quality": low_quality,
            "chars": len(text),
            "sentences": sentence_count,
            "has_bad_signal": has_bad_signal,
            "min_chars": min_chars,
            "min_sentences": min_sentences,
        }

    """检测工具链路失败信号（模型回复中显式暴露错误）。"""
    @staticmethod
    def _has_tool_failure_signal(content: str) -> bool:
        text = (content or "")
        signals = [
            "工具调用失败",
            "（工具调用出错",
            "MCP tools/call failed",
            "\"success\": false",
            "Error calling LLM",
        ]
        return any(sig in text for sig in signals)

    """收集本轮是否需要触发 Replan 的原因。"""
    def _collect_replan_reasons(
        self,
        mode: str,
        plan: Plan,
        response: ChatMessage,
    ) -> List[str]:
        reasons: List[str] = []
        meta = self._last_run_meta or {}

        # 存在写文件/写记忆副作用的分支不做 Replan，防止重复写入。
        if meta.get("has_side_effect"):
            return reasons

        if plan.need_rag and meta.get("retrieval_empty"):
            reasons.append("检索为空（索引缺失或未召回到有效片段）")

        if self._has_tool_failure_signal(response.content):
            reasons.append("工具失败（调用失败或工具错误信号）")

        qm = self._quality_metrics(response.content)
        if qm["low_quality"]:
            reasons.append(
                "回答质量偏低"
                f"(chars={qm['chars']}/{qm['min_chars']}, "
                f"sentences={qm['sentences']}/{qm['min_sentences']}, "
                f"bad_signal={int(qm['has_bad_signal'])})"
            )
        return reasons

    """按模式执行一次，作为 Replan 前后复用的统一分发入口。"""
    def _run_mode_once(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        plan: Plan,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None,
    ) -> ChatMessage:
        if mode == "learn":
            return self.run_learn_mode(course_name, user_message, plan, history)
        if mode == "practice":
            return self.run_practice_mode(course_name, user_message, plan, state, history)
        if mode == "exam":
            return self.run_exam_mode(course_name, user_message, plan, history)
        return ChatMessage(
            role="assistant",
            content=f"未知模式: {mode}",
            citations=None,
            tool_calls=None,
        )
    
    def get_workspace_path(self, course_name: str) -> str:
        """获取课程工作目录（包含路径穿越防护）。"""
        # 只取最后一个路径组件，防止 ../../../etc 等穿越攻击
        safe_name = os.path.basename(course_name.strip())
        if not safe_name or safe_name in (".", ".."):
            raise ValueError(f"无效的课程名称: {course_name!r}")
        return os.path.join(self.data_dir, safe_name)
    
    def load_retriever(self, course_name: str) -> Optional[Retriever]:
        """按课程加载检索器（未构建索引时返回 None）。"""
        workspace_path = self.get_workspace_path(course_name)
        index_path = os.path.abspath(os.path.join(workspace_path, "index", "faiss_index"))
        
        if not os.path.exists(f"{index_path}.faiss"):
            return None
        
        store = FAISSStore()
        store.load(index_path)
        return Retriever(store)

    @staticmethod
    def _top_k_for_mode(mode: str) -> int:
        m = (mode or "").strip().lower()
        if m == "exam":
            return int(os.getenv("RAG_TOPK_EXAM", "6"))
        if m in {"learn", "practice"}:
            return int(os.getenv("RAG_TOPK_LEARN_PRACTICE", "4"))
        return int(os.getenv("TOP_K_RESULTS", "3"))

    @staticmethod
    def _trim_history_recent(history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        if not history:
            return []
        turns = int(os.getenv("CB_HISTORY_RECENT_TURNS", "6"))
        keep = max(0, turns * 2)
        if keep <= 0:
            return []
        return history[-keep:]
    
    def run_learn_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None
    ) -> ChatMessage:
        """执行学习模式（非流式）。"""
        if history is None:
            history = []
        history = self._trim_history_recent(history)
        retrieval_empty = False
        # 关键步骤：按 plan 决定是否执行 RAG 检索并准备 citations。
        context = ""
        citations = []
        
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message, top_k=self._top_k_for_mode("learn"))
                if chunks:
                    context = retriever.format_context(chunks)
                    citations = chunks
                else:
                    retrieval_empty = True
                    context = "（检索未命中有效教材片段，本轮将基于通用知识和已有上下文回答）"
            else:
                retrieval_empty = True
                context = "（未找到相关教材，请先上传课程资料）"

        memory_ctx = self._fetch_history_ctx(
            query=user_message,
            course_name=course_name,
            mode="learn",
            agent="tutor",
            phase="answer",
        )
        packed = self.context_budgeter.build_context(
            query=user_message,
            history=history,
            rag_text=context,
            memory_text=memory_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
        )
        self._log_context_budget("learn", packed)
        context = packed["final_text"]
        
        # 关键步骤：组装 Tutor 入参并触发生成。
        workspace_path = self.get_workspace_path(course_name)
        notes_dir = os.path.abspath(os.path.join(workspace_path, "notes"))
        # 为 filewriter 工具注入当前课程的笔记目录
        from mcp_tools.client import MCPTools
        MCPTools._context = {"notes_dir": notes_dir}
        result: TutorResult = self.tutor.teach(
            user_message, course_name, context,
            allowed_tools=plan.allowed_tools,
            history=history,
        )
        # 质量检查：若回答过短或包含错误信号，自动重试一次
        if not self._check_quality(result.content):
            self.logger.info("[quality] retry_once=1 reason=low_quality%s", self._trace_tag())
            result = self.tutor.teach(
                user_message, course_name, context,
                allowed_tools=plan.allowed_tools,
                history=history,
            )

        # 合并 RAG citations 和 Tutor 内部工具调用产生的 citations
        merged_citations = citations + result.citations if citations else result.citations
        self._last_run_meta = {
            "mode": "learn",
            "retrieval_empty": retrieval_empty,
            "has_side_effect": False,
        }

        return ChatMessage(
            role="assistant",
            content=result.content,
            citations=merged_citations if merged_citations else None,
            tool_calls=None,
        )
    
    def run_practice_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None,
    ) -> ChatMessage:
        """对话式练习模式（非流式）：出题走 QuizMaster，交卷走 Grader。"""
        if history is None:
            history = []
        history = self._trim_history_recent(history)
        retrieval_empty = False

        context = ""
        citations = []
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message, top_k=self._top_k_for_mode("practice"))
                if chunks:
                    context = retriever.format_context(chunks)
                    citations = chunks
                else:
                    retrieval_empty = True
                    context = "（检索未命中有效教材片段，本轮将基于已有上下文出题）"
            else:
                retrieval_empty = True
                context = "（未找到相关教材，请先上传课程资料）"

        # 历史错题上下文
        history_ctx = self._fetch_history_ctx(
            query=user_message,
            course_name=course_name,
            mode="practice",
            agent="grader",
            phase="grade",
        )
        packed = self.context_budgeter.build_context(
            query=user_message,
            history=history,
            rag_text=context,
            memory_text=history_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
        )
        self._log_context_budget("practice", packed)
        context = packed["final_text"]
        history_ctx = packed["memory_text"]

        workspace_path = self.get_workspace_path(course_name)
        notes_dir = os.path.abspath(os.path.join(workspace_path, "notes"))
        MCPTools._context = {"notes_dir": notes_dir}
        tool_calls = None

        # ── 路由判断：答案提交 → GraderAgent；出题请求 → QuizMaster ──
        answer_submission = self._is_answer_submission(user_message, history)
        if answer_submission:
            self.logger.info("[route] practice answer_submission=1 target=grader%s", self._trace_tag())
            quiz_content = self._extract_quiz_from_history(history)
            chunks = []
            for chunk in self.grader.grade_practice_stream(
                quiz_content=quiz_content,
                student_answer=user_message,
                course_name=course_name,
                history_ctx=history_ctx,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            response_text = "".join(chunks)
            saved_path = self._save_practice_record(course_name, user_message, history, response_text)
            self._save_grading_to_memory(course_name, user_message, history, response_text)
            response_text += f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"
        else:
            topic, difficulty, num_questions, question_type = self._resolve_quiz_request(user_message)
            quiz = self.quizmaster.generate_quiz(
                course_name=course_name,
                topic=topic,
                difficulty=difficulty,
                context=context,
                num_questions=num_questions,
                question_type=question_type,
            )
            response_text = self._render_quiz_message(quiz)
            tool_calls = self._build_quiz_meta_tool_call(quiz)

        self._last_run_meta = {
            "mode": "practice",
            "retrieval_empty": retrieval_empty,
            "answer_submission": answer_submission,
            "has_side_effect": answer_submission,
        }

        return ChatMessage(
            role="assistant",
            content=response_text,
            citations=citations if citations else None,
            tool_calls=tool_calls,
        )

    def run_practice_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None,
    ):
        """对话式练习模式（流式）：交卷走 Grader 流式，出题走 QuizMaster。"""
        if history is None:
            history = []
        history = self._trim_history_recent(history)
        retrieval_empty = False

        context = ""
        citations_dicts = []
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message, top_k=self._top_k_for_mode("practice"))
                if chunks:
                    context = retriever.format_context(chunks)
                    citations_dicts = [c.model_dump() for c in chunks]
                else:
                    retrieval_empty = True
                    context = "（检索未命中有效教材片段，本轮将基于已有上下文出题）"
            else:
                retrieval_empty = True
                context = "（未找到相关教材，请先上传课程资料）"

        # 与 learn 模式保持一致：先发送 citations 事件给前端缓存
        if citations_dicts:
            yield {"__citations__": citations_dicts}

        history_ctx = self._fetch_history_ctx(
            query=user_message,
            course_name=course_name,
            mode="practice",
            agent="grader",
            phase="grade",
        )
        packed = self.context_budgeter.build_context(
            query=user_message,
            history=history,
            rag_text=context,
            memory_text=history_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
        )
        self._log_context_budget("practice", packed)
        context = packed["final_text"]
        history_ctx = packed["memory_text"]

        workspace_path = self.get_workspace_path(course_name)
        notes_dir = os.path.abspath(os.path.join(workspace_path, "notes"))
        MCPTools._context = {"notes_dir": notes_dir}

        # ── 路由判断：答案提交 → GraderAgent；出题请求 → QuizMaster ──
        answer_submission = self._is_answer_submission(user_message, history)
        if answer_submission:
            self.logger.info("[route] practice_stream answer_submission=1 target=grader%s", self._trace_tag())
            quiz_content = self._extract_quiz_from_history(history)
            collected = []
            for chunk in self.grader.grade_practice_stream(
                quiz_content=quiz_content,
                student_answer=user_message,
                course_name=course_name,
                history_ctx=history_ctx,
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
                yield chunk
            full_response = "".join(collected)
            saved_path = self._save_practice_record(course_name, user_message, history, full_response)
            self._save_grading_to_memory(course_name, user_message, history, full_response)
            yield f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"
        else:
            topic, difficulty, num_questions, question_type = self._resolve_quiz_request(user_message)
            quiz = self.quizmaster.generate_quiz(
                course_name=course_name,
                topic=topic,
                difficulty=difficulty,
                context=context,
                num_questions=num_questions,
                question_type=question_type,
            )
            yield {"__tool_calls__": self._build_quiz_meta_tool_call(quiz)}
            yield self._render_quiz_message(quiz)

        self._last_run_meta = {
            "mode": "practice",
            "retrieval_empty": retrieval_empty,
            "answer_submission": answer_submission,
            "has_side_effect": answer_submission,
        }

    
    def run_exam_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: list = None,
    ) -> ChatMessage:
        """对话式考试模式（非流式）：出卷走 QuizMaster，交卷评分讲解走 Grader。"""
        if history is None:
            history = []
        history = self._trim_history_recent(history)
        retrieval_empty = False

        context = ""
        retriever = self.load_retriever(course_name)
        if retriever:
            chunks = retriever.retrieve(user_message, top_k=self._top_k_for_mode("exam"))
            if chunks:
                context = retriever.format_context(chunks)
            else:
                retrieval_empty = True
                context = "（检索未命中有效教材片段，本轮将基于已有上下文生成试卷/评分）"
        else:
            retrieval_empty = True
            context = "（未找到相关教材，请先上传课程资料）"

        history_ctx = self._fetch_history_ctx(
            query=user_message,
            course_name=course_name,
            mode="exam",
            agent="grader",
            phase="grade",
        )
        packed = self.context_budgeter.build_context(
            query=user_message,
            history=history,
            rag_text=context,
            memory_text=history_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
        )
        self._log_context_budget("exam", packed)
        context = packed["final_text"]
        history_ctx = packed["memory_text"]
        answer_submission = self._is_exam_answer_submission(user_message, history)
        tool_calls = None
        if answer_submission:
            exam_paper = self._extract_exam_from_history(history)
            chunks = []
            for chunk in self.grader.grade_exam_stream(
                exam_paper=exam_paper,
                student_answer=user_message,
                course_name=course_name,
                history_ctx=history_ctx,
            ):
                if isinstance(chunk, str):
                    chunks.append(chunk)
            response_text = "".join(chunks)
            saved_path = self._save_exam_record(course_name, user_message, history, response_text)
            self._save_exam_to_memory(course_name, response_text)
            response_text += f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"
        else:
            exam_payload = self.quizmaster.generate_exam_paper(
                course_name=course_name,
                user_request=user_message,
                context=context,
            )
            if not isinstance(exam_payload, dict):
                exam_payload = {"content": str(exam_payload), "answer_sheet": [], "total_score": 0}
            response_text = str(exam_payload.get("content", "")).strip()
            tool_calls = self._build_exam_meta_tool_call(
                exam_payload.get("answer_sheet", []),
                int(exam_payload.get("total_score", 0)),
            )

        self._last_run_meta = {
            "mode": "exam",
            "retrieval_empty": retrieval_empty,
            "exam_grading": answer_submission,
            "has_side_effect": answer_submission,
        }

        return ChatMessage(
            role="assistant",
            content=response_text,
            citations=None,
            tool_calls=tool_calls,
        )

    def run_exam_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: list = None,
    ):
        """对话式考试模式（流式）：交卷走 Grader 流式，出卷走 QuizMaster。"""
        if history is None:
            history = []
        history = self._trim_history_recent(history)
        retrieval_empty = False

        context = ""
        citations_dicts = []
        retriever = self.load_retriever(course_name)
        if retriever:
            chunks = retriever.retrieve(user_message, top_k=self._top_k_for_mode("exam"))
            if chunks:
                context = retriever.format_context(chunks)
                citations_dicts = [c.model_dump() for c in chunks]
            else:
                retrieval_empty = True
                context = "（检索未命中有效教材片段，本轮将基于已有上下文生成试卷/评分）"
        else:
            retrieval_empty = True
            context = "（未找到相关教材，请先上传课程资料）"

        # 与 learn 模式保持一致：先发送 citations 事件给前端缓存
        if citations_dicts:
            yield {"__citations__": citations_dicts}

        history_ctx = self._fetch_history_ctx(
            query=user_message,
            course_name=course_name,
            mode="exam",
            agent="grader",
            phase="grade",
        )
        packed = self.context_budgeter.build_context(
            query=user_message,
            history=history,
            rag_text=context,
            memory_text=history_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
        )
        self._log_context_budget("exam", packed)
        context = packed["final_text"]
        history_ctx = packed["memory_text"]
        answer_submission = self._is_exam_answer_submission(user_message, history)
        if answer_submission:
            exam_paper = self._extract_exam_from_history(history)
            collected = []
            for chunk in self.grader.grade_exam_stream(
                exam_paper=exam_paper,
                student_answer=user_message,
                course_name=course_name,
                history_ctx=history_ctx,
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
                yield chunk
            full_response = "".join(collected)
            saved_path = self._save_exam_record(course_name, user_message, history, full_response)
            self._save_exam_to_memory(course_name, full_response)
            yield f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"
        else:
            exam_payload = self.quizmaster.generate_exam_paper(
                course_name=course_name,
                user_request=user_message,
                context=context,
            )
            if not isinstance(exam_payload, dict):
                exam_payload = {"content": str(exam_payload), "answer_sheet": [], "total_score": 0}
            yield {
                "__tool_calls__": self._build_exam_meta_tool_call(
                    exam_payload.get("answer_sheet", []),
                    int(exam_payload.get("total_score", 0)),
                )
            }
            yield str(exam_payload.get("content", "")).strip()

        self._last_run_meta = {
            "mode": "exam",
            "retrieval_empty": retrieval_empty,
            "exam_grading": answer_submission,
            "has_side_effect": answer_submission,
        }

    def _save_mistake(
        self,
        course_name: str,
        quiz: Quiz,
        student_answer: str,
        grade_report: GradeReport
    ):
        """Save mistake to log."""
        workspace_path = self.get_workspace_path(course_name)
        mistakes_dir = os.path.join(workspace_path, "mistakes")
        os.makedirs(mistakes_dir, exist_ok=True)
        
        mistake_file = os.path.join(mistakes_dir, "mistakes.jsonl")
        
        mistake_entry = {
            "timestamp": datetime.now().isoformat(),
            "question": quiz.question,
            "student_answer": student_answer,
            "standard_answer": quiz.standard_answer,
            "score": grade_report.score,
            "feedback": grade_report.feedback,
            "mistake_tags": grade_report.mistake_tags
        }
        
        with open(mistake_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(mistake_entry, ensure_ascii=False) + '\n')

    # ------------------------------------------------------------------ #
    #  记录检测 & 自动保存辅助方法
    # ------------------------------------------------------------------ #

    def _check_quality(self, content: str) -> bool:
        """检查 Tutor 回答质量，过短或含错误信号则返回 False。"""
        if not content or len(content.strip()) < 150:
            return False
        error_signals = ["Error calling LLM", "工具调用失败", "请重试", "API Error"]
        return not any(sig in content for sig in error_signals)

    def _fetch_history_ctx(
        self,
        query: str,
        course_name: str,
        mode: str = "",
        agent: str = "",
        phase: str = "",
    ) -> str:
        """从记忆库预取历史错题片段，返回可追加到 system prompt 的字符串。"""
        try:
            top_k = int(os.getenv("CB_MEMORY_TOPK", "2"))
            item_max_chars = int(os.getenv("CB_MEMORY_ITEM_MAX_CHARS", "100"))
            cache = getattr(self, "_tool_dedup_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._tool_dedup_cache = cache
            key_payload = {
                "tool": "memory_search",
                "query": query,
                "course_name": course_name,
                "event_types": ["mistake", "practice", "exam", "qa"],
                "mode": mode or None,
                "agent": agent or None,
                "phase": phase or None,
                "top_k": top_k,
            }
            cache_key = json.dumps(key_payload, ensure_ascii=False, sort_keys=True)
            if cache_key in cache:
                mem = cache[cache_key]
                self.logger.info("[tool_dedup] memory_search hit=1 reason=runner_cache%s", self._trace_tag())
            else:
                mem = MCPTools.call_tool(
                    "memory_search",
                    query=query,
                    course_name=course_name,
                    event_types=["mistake", "practice", "exam", "qa"],
                    mode=mode or None,
                    agent=agent or None,
                    phase=phase or None,
                    top_k=top_k,
                )
                cache[cache_key] = mem
            if mem.get("success") and mem.get("results"):
                snippets = []
                for r in mem["results"][: max(1, top_k)]:
                    text = ""
                    if isinstance(r, dict):
                        text = (
                            r.get("content")
                            or r.get("summary")
                            or r.get("text")
                            or ""
                        )
                    elif isinstance(r, str):
                        text = r
                    text = text.strip()
                    if text:
                        snippets.append(text[: max(20, item_max_chars)])
                if snippets:
                    return (
                        "\n\n【该知识点历史错题参考（评分时请特别关注相同薄弱点）】\n"
                        + "\n".join(f"- {s}" for s in snippets)
                    )
        except Exception:
            pass
        return ""

    @staticmethod
    def _log_context_budget(mode: str, packed: Dict[str, Any]) -> None:
        history_tokens = int(packed.get("history_tokens_est", 0) or 0)
        rag_tokens = int(packed.get("rag_tokens_est", 0) or 0)
        memory_tokens = int(packed.get("memory_tokens_est", 0) or 0)
        final_tokens = int(packed.get("final_tokens_est", 0) or 0)
        budget_tokens = int(packed.get("budget_tokens_est", 0) or 0)
        logging.getLogger("runner").info(
            "[context_budget] mode=%s history=%d rag=%d memory=%d final=%d budget=%d",
            mode,
            history_tokens,
            rag_tokens,
            memory_tokens,
            final_tokens,
            budget_tokens,
        )

    """从用户出题请求中解析主题、难度、题量与题型（轻量规则，失败回退默认值）。"""
    @staticmethod
    def _resolve_quiz_request(user_message: str) -> tuple[str, str, int, str]:
        import re

        text = (user_message or "").strip()
        if not text:
            return "当前课程核心知识点", "medium", 1, "简答题"

        lowered = text.lower()
        difficulty = "medium"
        if any(k in text for k in ["简单", "基础", "入门"]) or "easy" in lowered:
            difficulty = "easy"
        elif any(k in text for k in ["困难", "综合", "挑战"]) or "hard" in lowered:
            difficulty = "hard"
        elif any(k in text for k in ["中等", "普通"]) or "medium" in lowered:
            difficulty = "medium"

        # 解析题量：支持阿拉伯数字与常见中文数字（默认 1，最大 20）。
        num_questions = 1
        count_match = re.search(r"(\d{1,2})\s*(?:道|题|个|条)", text)
        if count_match:
            num_questions = int(count_match.group(1))
        else:
            zh_map = {
                "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
                "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
                "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
            }
            for zh, value in sorted(zh_map.items(), key=lambda x: len(x[0]), reverse=True):
                if re.search(fr"{zh}\s*(?:道|题|个|条)", text):
                    num_questions = value
                    break
        num_questions = max(1, min(num_questions, 20))

        # 解析题型：未指定时交给命题器自动决定。
        question_type = "综合题"
        if any(k in text for k in ["判断题", "判断"]):
            question_type = "判断题"
        elif any(k in text for k in ["单选", "选择题", "多选"]):
            question_type = "选择题"
        elif "填空" in text:
            question_type = "填空题"
        elif "简答" in text:
            question_type = "简答题"
        elif "论述" in text:
            question_type = "论述题"
        elif "计算" in text:
            question_type = "计算题"

        topic = text
        # 去除常见口语前缀，保留核心主题短语
        for prefix in ["给我出", "帮我出", "请出", "出", "来", "我想练习"]:
            if topic.startswith(prefix):
                topic = topic[len(prefix):].strip()
        topic = re.sub(r"\d{1,2}\s*(?:道|题|个|条)", "", topic)
        topic = re.sub(r"(?:一|二|两|三|四|五|六|七|八|九|十|十一|十二|十三|十四|十五|十六|十七|十八|十九|二十)\s*(?:道|题|个|条)", "", topic)
        topic = re.sub(r"(判断题|选择题|单选题|多选题|填空题|简答题|论述题|计算题)", "", topic)
        topic = re.sub(r"^(关于|有关)", "", topic).strip()
        if topic.endswith("的") and len(topic) > 2:
            topic = topic[:-1].strip()
        topic = re.sub(r"[，,。；;!！]+$", "", topic).strip()
        topic = topic[:120] if topic else text[:120]
        return topic, difficulty, num_questions, question_type

    """将 Quiz 结构渲染为学生可见题面。"""
    @staticmethod
    def _render_quiz_message(quiz: Quiz) -> str:
        return (
            "## 练习题\n\n"
            f"{quiz.question}\n\n"
            "请回答上述题目，回答完毕后我会为你评分并给出详细讲解。"
        )

    """将 Quiz 元数据挂载到内部 tool_calls，避免污染正文显示。"""
    @staticmethod
    def _build_quiz_meta_tool_call(quiz: Quiz) -> List[Dict[str, Any]]:
        return [
            {
                "type": "internal_meta",
                "name": "quiz_meta",
                "payload": {
                    "question": quiz.question,
                    "standard_answer": quiz.standard_answer,
                    "rubric": quiz.rubric,
                    "chapter": quiz.chapter,
                    "concept": quiz.concept,
                    "difficulty": quiz.difficulty,
                },
            }
        ]

    """将考试答案表挂载到内部 tool_calls，供交卷评分阶段使用。"""
    @staticmethod
    def _build_exam_meta_tool_call(answer_sheet: List[Dict[str, Any]], total_score: int) -> List[Dict[str, Any]]:
        return [
            {
                "type": "internal_meta",
                "name": "exam_meta",
                "payload": {
                    "answer_sheet": answer_sheet or [],
                    "total_score": int(total_score or 0),
                },
            }
        ]

    """从历史里提取内部元数据。"""
    @staticmethod
    def _extract_internal_meta(history: list, name: str) -> Optional[Dict[str, Any]]:
        for msg in reversed(history[-30:]):
            if msg.get("role") != "assistant":
                continue
            tcs = msg.get("tool_calls") or []
            if not isinstance(tcs, list):
                continue
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                if tc.get("type") == "internal_meta" and tc.get("name") == name:
                    payload = tc.get("payload")
                    if isinstance(payload, dict):
                        return payload
        return None

    def _is_answer_submission(self, user_message: str, history: list) -> bool:
        """检测用户是否在提交答案（而非请求出题）。

        判断依据：
        1. 用户消息含答案提交特征词或答案编号格式。
        2. 历史中最近一条 assistant 消息包含题目结构。
        """
        import re
        answer_markers = [
            "第1题", "第一题", "我的答案", "答案如下", "提交答案", "答：",
        ]
        has_answer_marker = any(m in user_message for m in answer_markers)
        # 纯编号+答案组合（如 "1.A  2.B  3.正确"）
        if re.search(r'[1-9][.、：:]\s*[A-Za-z正确错误√×对]', user_message):
            has_answer_marker = True
        # 多行都有 "第X题" 或 "X." 编号
        if len(re.findall(r'(?:第\d+题|^\d+[.、])', user_message, re.MULTILINE)) >= 2:
            has_answer_marker = True

        # 最近 assistant 消息是否含题目结构
        has_quiz_in_history = bool(self._extract_internal_meta(history, "quiz_meta"))
        if not has_quiz_in_history:
            for msg in reversed(history[-12:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    quiz_signals = ["题目", "选择题", "判断题", "填空题", "简答题",
                                    "第1题", "第一题", "标准答案", "答案选", "下列哪", "以下哪"]
                    if sum(1 for kw in quiz_signals if kw in content) >= 2:
                        has_quiz_in_history = True
                    break

        if not has_quiz_in_history:
            return False

        # 放宽判定：已出题后，像“错误/正确/A”这类短答案也应进入评分阶段。
        normalized = (user_message or "").strip().lower()
        simple_answers = {
            "a", "b", "c", "d", "ab", "ac", "ad", "bc", "bd", "cd", "abcd",
            "对", "错", "正确", "错误", "true", "false", "√", "×",
        }
        if normalized in simple_answers:
            return True

        compact_answer = re.fullmatch(
            r"(?:[a-dA-D]|正确|错误|对|错|√|×)(?:[\s,，、;/；]+(?:[a-dA-D]|正确|错误|对|错|√|×)){0,19}",
            (user_message or "").strip(),
        )
        if compact_answer:
            return True

        request_like = any(
            k in user_message for k in [
                "出题", "再来", "下一题", "继续出", "给我出", "帮我出", "来一道",
                "练习题", "判断题", "选择题", "填空题", "简答题", "论述题", "计算题",
            ]
        )
        if re.search(
            r"(?:给我|帮我|请)?\s*(?:再)?出\s*(?:[一二两三四五六七八九十\d]+\s*)?(?:道|题|个|条)",
            user_message,
        ):
            request_like = True
        if re.search(r"(来|再来)\s*一\s*(?:道|题)", user_message):
            request_like = True
        if re.search(r"下一\s*题", user_message):
            request_like = True
        ask_like = any(
            k in user_message for k in ["什么", "为什么", "怎么", "解释", "提示", "再讲", "请讲"]
        ) or ("?" in user_message or "？" in user_message)

        if has_answer_marker:
            return True
        if len((user_message or "").strip()) <= 24 and not request_like and not ask_like:
            return True

        return False

    """检测考试模式是否为“提交答案”阶段。"""
    def _is_exam_answer_submission(self, user_message: str, history: list) -> bool:
        import re

        answer_markers = [
            "第1题", "第一题", "我的答案", "答案如下", "提交答案", "答：",
        ]
        has_answer_marker = any(m in user_message for m in answer_markers)
        if re.search(r'[1-9][.、：:]\s*[A-Za-z正确错误√×对]', user_message):
            has_answer_marker = True
        if len(re.findall(r'(?:第\d+题|^\d+[.、])', user_message, re.MULTILINE)) >= 2:
            has_answer_marker = True

        has_exam_in_history = bool(self._extract_internal_meta(history, "exam_meta"))
        if not has_exam_in_history:
            for msg in reversed(history[-20:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    exam_signals = ["模拟考试试卷", "考试须知", "第一部分", "总分"]
                    if sum(1 for kw in exam_signals if kw in content) >= 2:
                        has_exam_in_history = True
                    break
        return has_answer_marker and has_exam_in_history

    def _extract_quiz_from_history(self, history: list) -> str:
        """从历史中提取最近一条 assistant 出题消息作为题目原文。"""
        meta = self._extract_internal_meta(history, "quiz_meta")
        if meta:
            question = str(meta.get("question", "")).strip()
            standard_answer = str(meta.get("standard_answer", "")).strip()
            rubric = str(meta.get("rubric", "")).strip()
            chapter = str(meta.get("chapter", "")).strip()
            concept = str(meta.get("concept", "")).strip()
            difficulty = str(meta.get("difficulty", "")).strip()
            parts = [
                "【题目】",
                question or "（题干缺失）",
                "",
                "【标准答案】",
                standard_answer or "（标准答案缺失）",
                "",
                "【评分标准】",
                rubric or "（评分标准缺失）",
            ]
            if chapter:
                parts.extend(["", f"【章节】{chapter}"])
            if concept:
                parts.extend([f"【知识点】{concept}"])
            if difficulty:
                parts.extend([f"【难度】{difficulty}"])
            return "\n".join(parts)

        for msg in reversed(history[-12:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                quiz_signals = ["题目", "选择题", "判断题", "填空题", "简答题",
                                "第1题", "第一题", "标准答案", "答案选", "下列哪", "以下哪"]
                if sum(1 for kw in quiz_signals if kw in content) >= 2:
                    return content
        return "（未能从历史中提取题目，请检查对话上下文）"

    """从历史中提取最近一份考试试卷原文。"""
    def _extract_exam_from_history(self, history: list) -> str:
        meta = self._extract_internal_meta(history, "exam_meta")
        if meta:
            visible_exam = ""
            for msg in reversed(history[-20:]):
                if msg.get("role") == "assistant":
                    content = msg.get("content", "")
                    signals = ["模拟考试试卷", "第一部分", "考试须知"]
                    if sum(1 for kw in signals if kw in content) >= 2:
                        visible_exam = content
                        break
            hidden_meta = {
                "answer_sheet": meta.get("answer_sheet", []) or [],
                "total_score": int(meta.get("total_score", 0) or 0),
            }
            if not visible_exam:
                visible_exam = "# 模拟考试试卷\n\n（未在历史中提取到试卷正文）"
            return (
                f"{visible_exam}\n\n"
                "[INTERNAL_EXAM_META]\n"
                f"{json.dumps(hidden_meta, ensure_ascii=False)}"
            )

        for msg in reversed(history[-20:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                signals = ["模拟考试试卷", "第一部分", "考试须知"]
                if sum(1 for kw in signals if kw in content) >= 2:
                    return content
        return "（未能从历史中提取试卷，请检查对话上下文）"

    def _is_practice_grading(self, text: str) -> bool:
        """判断练习模式回复是否为评分阶段。"""
        keywords = ["评分结果", "标准解析", "易错提醒", "得分", "答对的部分", "需要改进", "逐题核对", "标准答案", "学生答案"]
        return sum(1 for kw in keywords if kw in text) >= 2

    def _save_grading_to_memory(
        self,
        course_name: str,
        user_answer: str,
        history: list,
        response_text: str,
    ) -> None:
        """将练习评分结果写入情景记忆，使用 PracticeGradeSignal 提取结构化信息。"""
        try:
            from memory.manager import get_memory_manager

            # 提取题目（历史中最近一条 assistant 消息）
            question_summary = "（未能提取题目）"
            for msg in reversed(history[-20:]):
                if msg.get("role") == "assistant":
                    question_summary = msg.get("content", "")[:300]
                    break

            # 用结构化方法解析评分和错误标签，替代原内联 regex
            signal = PracticeGradeSignal.from_text(
                response_text=response_text,
                student_answer=user_answer,
                question_summary=question_summary,
            )

            content = (
                f"题目: {signal.question_summary}\n"
                f"学生答案: {signal.student_answer}\n"
                f"得分: {signal.score:.0f}"
            )
            if signal.mistake_tags:
                content += f"\n错误类型: {', '.join(signal.mistake_tags)}"

            mgr = get_memory_manager()
            mgr.record_event(
                course_name=course_name,
                event_type="mistake" if signal.is_mistake else "practice",
                content=content,
                importance=0.9 if signal.is_mistake else 0.4,
                metadata={"score": signal.score, "tags": signal.mistake_tags},
                score=signal.score,
                concepts=signal.mistake_tags,
                update_weak_points=signal.is_mistake,
                increment_practice=True,
                mode="practice",
                agent="grader",
                phase="grade",
            )
            self.logger.info(
                "[memory] practice_saved type=%s score=%.0f%s",
                "mistake" if signal.is_mistake else "practice",
                signal.score,
                self._trace_tag(),
            )
        except Exception as _e:
            self.logger.warning("[memory] practice_save_failed err=%s%s", str(_e), self._trace_tag())

    def _save_practice_record(self, course_name: str, user_message: str, history: list, response_text: str) -> str:
        """保存练习题记录（题目、用户答案、评分解析），返回相对路径。
        user_message: 当前用户提交的答案（直接传入，不从 history 提取）
        history: 当前消息之前的历史（用于提取题目内容）
        """
        workspace_path = self.get_workspace_path(course_name)
        practices_dir = os.path.join(workspace_path, "practices")
        os.makedirs(practices_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"练习记录_{timestamp}.md"
        filepath = os.path.join(practices_dir, filename)

        # 从历史中提取最近一条 assistant 消息作为题目内容
        quiz_content = None
        for msg in reversed(history[-20:]):
            if msg.get("role") == "assistant":
                quiz_content = msg.get("content", "")
                break

        md = f"""# 练习记录

**时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**课程**：{course_name}

---

## 题目

{quiz_content or '（未能提取题目内容）'}

---

## 我的答案

{user_message}

---

## 评分与详细解析

{response_text}
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md)
        return f"practices/{filename}"

    def _save_exam_record(self, course_name: str, user_message: str, history: list, response_text: str) -> str:
        """保存考试完整记录（试卷、用户答案、批改报告），返回相对路径。
        user_message: 用户提交的全部答案（直接传入）
        history: 当前消息之前的历史（用于提取试卷内容）
        """
        workspace_path = self.get_workspace_path(course_name)
        exams_dir = os.path.join(workspace_path, "exams")
        os.makedirs(exams_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"考试记录_{timestamp}.md"
        filepath = os.path.join(exams_dir, filename)

        # 从历史中提取包含试卷内容的最近 assistant 消息
        exam_paper = None
        for msg in reversed(history[-30:]):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if any(kw in content for kw in ["模拟考试试卷", "第一部分", "第二部分"]):
                    exam_paper = content
                    break

        md = f"""# 考试记录

**时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**课程**：{course_name}

---

## 试卷

{exam_paper or '（未能提取试卷内容）'}

---

## 我的答案

{user_message}

---

## 批改报告

{response_text}
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(md)
        return f"exams/{filename}"

    def _save_exam_to_memory(self, course_name: str, response_text: str) -> None:
        """将考试批改结果写入情景记忆，并同步薄弱知识点到用户画像。"""
        try:
            import re
            from memory.manager import get_memory_manager

            # 提取总分（兼容“总得分：88 / 100”、“总分: 72分”等写法）
            score = None
            score_patterns = [
                r"(?:总得分|总分)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*/\s*100",
                r"(?:总得分|总分)[：:\s]*([0-9]+(?:\.[0-9]+)?)\s*分",
            ]
            for pattern in score_patterns:
                m = re.search(pattern, response_text)
                if m:
                    score = float(m.group(1))
                    break

            # 提取薄弱知识点（兼容“薄弱知识点：A、B”与项目符号列表）
            weak_points: List[str] = []
            block = re.search(
                r"薄弱知识点[：:\s]*([\s\S]{0,300})(?:\n## |\n---|\Z)",
                response_text,
            )
            if block:
                section = block.group(1)
                bullet_items = re.findall(r"(?:^|\n)\s*[-*•]\s*([^\n]{1,40})", section)
                if bullet_items:
                    weak_points = [x.strip() for x in bullet_items if x.strip()]
                else:
                    inline = re.sub(r"[\r\n]+", " ", section).strip()
                    weak_points = [
                        x.strip()
                        for x in re.split(r"[,，、；;]", inline)
                        if x.strip()
                    ]
            weak_points = weak_points[:8]

            # 生成可检索摘要，避免把完整长文原样入库
            excerpt = response_text.strip().replace("\r", "")
            excerpt = re.sub(r"\n{3,}", "\n\n", excerpt)[:900]
            content = "考试批改摘要：\n" + excerpt
            if score is not None:
                content = f"考试总分: {score:.0f}/100\n" + content
            if weak_points:
                content += f"\n薄弱知识点: {', '.join(weak_points)}"

            mgr = get_memory_manager()
            importance = 0.9 if (score is not None and score < 60) else 0.6
            mgr.record_event(
                course_name=course_name,
                event_type="exam",
                content=content,
                importance=importance,
                metadata={"score": score, "weak_points": weak_points},
                score=score,
                concepts=weak_points,
                update_weak_points=bool(weak_points),
                mode="exam",
                agent="grader",
                phase="grade",
            )
            self.logger.info(
                "[memory] exam_saved score=%s%s",
                score if score is not None else "N/A",
                self._trace_tag(),
            )
        except Exception as _e:
            self.logger.warning("[memory] exam_save_failed err=%s%s", str(_e), self._trace_tag())

    def run(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None
    ) -> tuple[ChatMessage, Plan]:
        """主编排入口（非流式）。"""
        if history is None:
            history = []
        self._tool_dedup_cache = {}
        # 关键步骤：先由 Router 产出本轮执行计划（是否检索、允许工具等）。
        plan = self.router.plan(user_message, mode, course_name)

        # 先执行一次主流程，再根据执行信号决定是否单次 Replan。
        response = self._run_mode_once(course_name, mode, user_message, plan, state, history)
        enable_replan = os.getenv("ENABLE_ROUTER_REPLAN", "1") == "1"
        if enable_replan:
            reasons = self._collect_replan_reasons(mode, plan, response)
            if reasons:
                reason_text = "；".join(reasons)
                self.logger.info("[replan] trigger=1 mode=%s reasons=%s%s", mode, reason_text, self._trace_tag())
                new_plan = self.router.replan(
                    user_message=user_message,
                    mode=mode,
                    course_name=course_name,
                    previous_plan=plan,
                    reason=reason_text,
                )
                if new_plan.model_dump() != plan.model_dump():
                    self.logger.info("[replan] plan_changed=1 rerun=1%s", self._trace_tag())
                    plan = new_plan
                    response = self._run_mode_once(course_name, mode, user_message, plan, state, history)
                else:
                    self.logger.info("[replan] plan_changed=0 skip_rerun=1%s", self._trace_tag())

        return response, plan

    def run_learn_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None
    ):
        """流式学习模式：先检索上下文，再流式输出导师回答。

        首先 yield 一个特殊事件 {"__citations__": [...]} 供前端捕获并展示引用框。
        后续所有 yield 均为文本 chunk。
        """
        if history is None:
            history = []
        history = self._trim_history_recent(history)

        context = ""
        citations_dicts = []
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message, top_k=self._top_k_for_mode("learn"))
                context = retriever.format_context(chunks)
                citations_dicts = [c.model_dump() for c in chunks]
            else:
                context = "（未找到相关教材，请先上传课程资料）"

        memory_ctx = self._fetch_history_ctx(
            query=user_message,
            course_name=course_name,
            mode="learn",
            agent="tutor",
            phase="answer",
        )
        packed = self.context_budgeter.build_context(
            query=user_message,
            history=history,
            rag_text=context,
            memory_text=memory_ctx,
            rag_sent_per_chunk=int(os.getenv("CB_RAG_SENT_PER_CHUNK", "2")),
            rag_sent_max_chars=int(os.getenv("CB_RAG_SENT_MAX_CHARS", "120")),
        )
        self._log_context_budget("learn", packed)
        context = packed["final_text"]

        # 先发送 citations 事件（前端按 __citations__ key 识别，不会渲染为文本）
        if citations_dicts:
            yield {"__citations__": citations_dicts}

        workspace_path = self.get_workspace_path(course_name)
        notes_dir = os.path.abspath(os.path.join(workspace_path, "notes"))
        MCPTools._context = {"notes_dir": notes_dir}

        yield from self.tutor.teach_stream(
            user_message, course_name, context,
            allowed_tools=plan.allowed_tools,
            history=history
        )

        # 流式输出完成后写入情景记忆（异步失败不影响主流程）
        try:
            from memory.manager import get_memory_manager
            mgr = get_memory_manager()
            doc_ids = [c["doc_id"] for c in citations_dicts] if citations_dicts else []
            content = f"问题: {user_message}"
            if doc_ids:
                content += f"\n参考来源: {', '.join(dict.fromkeys(doc_ids))}"
            mgr.record_event(
                course_name=course_name,
                event_type="qa",
                content=content,
                importance=0.5,
                metadata={"doc_ids": doc_ids},
                increment_qa=True,
                mode="learn",
                agent="tutor",
                phase="answer",
            )
        except Exception as _mem_err:
            self.logger.warning("[memory] learn_qa_save_failed err=%s%s", str(_mem_err), self._trace_tag())

    def run_stream(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None
    ):
        """主流式入口，learn 模式真正流式，其他模式一次性输出。"""
        if history is None:
            history = []
        self._tool_dedup_cache = {}
        plan = self.router.plan(user_message, mode, course_name)
        enable_replan = os.getenv("ENABLE_ROUTER_REPLAN", "1") == "1"
        if enable_replan and plan.need_rag:
            # 流式场景不宜在输出后重跑；仅在开流前做一次“索引缺失”预重规划。
            if self.load_retriever(course_name) is None:
                reason = "检索为空（索引缺失或未构建）"
                new_plan = self.router.replan(
                    user_message=user_message,
                    mode=mode,
                    course_name=course_name,
                    previous_plan=plan,
                    reason=reason,
                )
                if new_plan.model_dump() != plan.model_dump():
                    self.logger.info("[replan] stream_precheck mode=%s reason=%s%s", mode, reason, self._trace_tag())
                    plan = new_plan

        if mode == "learn":
            yield from self.run_learn_mode_stream(course_name, user_message, plan, history)
        elif mode == "practice":
            yield from self.run_practice_mode_stream(course_name, user_message, plan, history)
        elif mode == "exam":
            yield from self.run_exam_mode_stream(course_name, user_message, plan, history)
        else:
            response, _ = self.run(course_name, mode, user_message, state, history)
            yield response.content
