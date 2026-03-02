"""Main orchestration runner."""
import os
import json
from typing import Dict, Any, List, Optional
from datetime import datetime

from backend.schemas import (
    Plan, ChatMessage, RetrievedChunk, Quiz, GradeReport,
    TutorResult, PracticeGradeSignal
)
from core.agents.router import RouterAgent
from core.agents.tutor import TutorAgent
from core.agents.quizmaster import QuizMasterAgent
from core.agents.grader import GraderAgent
from rag.retrieve import Retriever
from rag.store_faiss import FAISSStore
from mcp_tools.client import MCPTools
from core.orchestration.prompts import PRACTICE_PROMPT, EXAM_PROMPT


class OrchestrationRunner:
    """Main orchestration runner for the course agent system."""
    
    def __init__(self, data_dir: str = None):
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR", "./data/workspaces")
        self.data_dir = data_dir
        
        # Initialize agents
        self.router = RouterAgent()
        self.tutor = TutorAgent()
        self.quizmaster = QuizMasterAgent()
        self.grader = GraderAgent()
        self.tools = MCPTools()
    
    def get_workspace_path(self, course_name: str) -> str:
        """Get workspace path for a course. Sanitizes course_name to prevent path traversal."""
        # 只取最后一个路径组件，防止 ../../../etc 等穿越攻击
        safe_name = os.path.basename(course_name.strip())
        if not safe_name or safe_name in (".", ".."):
            raise ValueError(f"无效的课程名称: {course_name!r}")
        return os.path.join(self.data_dir, safe_name)
    
    def load_retriever(self, course_name: str) -> Optional[Retriever]:
        """Load retriever for a course."""
        workspace_path = self.get_workspace_path(course_name)
        index_path = os.path.abspath(os.path.join(workspace_path, "index", "faiss_index"))
        
        if not os.path.exists(f"{index_path}.faiss"):
            return None
        
        store = FAISSStore()
        store.load(index_path)
        return Retriever(store)
    
    def run_learn_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None
    ) -> ChatMessage:
        """Execute learn mode."""
        if history is None:
            history = []
        # Retrieve context if needed
        context = ""
        citations = []
        
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message)
                context = retriever.format_context(chunks)
                citations = chunks
            else:
                context = "（未找到相关教材，请先上传课程资料）"
        
        # Generate teaching response
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
            print("[QualityCheck] 回答质量不足，自动重试")
            result = self.tutor.teach(
                user_message, course_name, context,
                allowed_tools=plan.allowed_tools,
                history=history,
            )

        # 合并 RAG citations 和 Tutor 内部工具调用产生的 citations
        merged_citations = citations + result.citations if citations else result.citations

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
        """对话式练习模式：LLM 根据历史自动判断出题/评分，无需 state。"""
        if history is None:
            history = []

        context = ""
        citations = []
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message)
                context = retriever.format_context(chunks)
                citations = chunks
            else:
                context = "（未找到相关教材，请先上传课程资料）"

        prompt = PRACTICE_PROMPT.format(
            course_name=course_name,
            context=context,
            question=user_message,
        )

        # 预查询历史错题，注入评分上下文
        history_ctx = ""
        try:
            mem = MCPTools.call_tool("memory_search", query=user_message, course_name=course_name)
            if mem.get("success") and mem.get("results"):
                snippets = [
                    r.get("content", "")[:120]
                    for r in mem["results"][:2]
                    if r.get("content")
                ]
                if snippets:
                    history_ctx = "\n\n【该知识点历史错题参考（评分时请特别关注相同薄弱点）】\n" + "\n".join(f"- {s}" for s in snippets)
        except Exception:
            pass

        sys_content = "你是一位专业的课程练习导师，负责出题、评分和讲解。严格按照用户提示词中的对话规则执行。" + history_ctx
        messages: List[dict] = [
            {"role": "system", "content": sys_content}
        ]
        for msg in history[-20:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})

        llm = self.tutor.llm
        response_text = llm.chat(messages, temperature=0.7, max_tokens=2000)

        # 评分阶段自动保存记录
        if self._is_practice_grading(response_text):
            saved_path = self._save_practice_record(course_name, user_message, history, response_text)
            self._save_grading_to_memory(course_name, user_message, history, response_text)
            response_text += f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"

        return ChatMessage(
            role="assistant",
            content=response_text,
            citations=citations if citations else None,
            tool_calls=None,
        )

    def run_practice_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: List[Dict[str, str]] = None,
    ):
        """对话式练习模式流式版本。"""
        if history is None:
            history = []

        context = ""
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message)
                context = retriever.format_context(chunks)
            else:
                context = "（未找到相关教材，请先上传课程资料）"

        prompt = PRACTICE_PROMPT.format(
            course_name=course_name,
            context=context,
            question=user_message,
        )

        # 预查询历史错题，注入评分上下文
        history_ctx = ""
        try:
            mem = MCPTools.call_tool("memory_search", query=user_message, course_name=course_name)
            if mem.get("success") and mem.get("results"):
                snippets = [
                    r.get("content", "")[:120]
                    for r in mem["results"][:2]
                    if r.get("content")
                ]
                if snippets:
                    history_ctx = "\n\n【该知识点历史错题参考（评分时请特别关注相同薄弱点）】\n" + "\n".join(f"- {s}" for s in snippets)
        except Exception:
            pass

        sys_content = "你是一位专业的课程练习导师，负责出题、评分和讲解。严格按照用户提示词中的对话规则执行。" + history_ctx
        messages: List[dict] = [
            {"role": "system", "content": sys_content}
        ]
        for msg in history[-20:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})

        llm = self.tutor.llm
        collected = []
        for chunk in llm.chat_stream(messages, temperature=0.7, max_tokens=2000):
            collected.append(chunk)
            yield chunk
        full_response = "".join(collected)
        if self._is_practice_grading(full_response):
            saved_path = self._save_practice_record(course_name, user_message, history, full_response)
            self._save_grading_to_memory(course_name, user_message, history, full_response)
            yield f"\n\n---\n📁 **本题记录已保存至**：`{saved_path}`"

    
    def run_exam_mode(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: list = None,
    ) -> ChatMessage:
        """对话式考试模式：LLM 根据历史自动判断出卷/评分。"""
        if history is None:
            history = []

        context = ""
        retriever = self.load_retriever(course_name)
        if retriever:
            chunks = retriever.retrieve(user_message, top_k=12)
            context = retriever.format_context(chunks)
        else:
            context = "（未找到相关教材，请先上传课程资料）"

        prompt = EXAM_PROMPT.format(
            course_name=course_name,
            context=context,
            question=user_message,
        )

        messages = [
            {"role": "system", "content": "你是一位严肃公正的考试主考官，严格按照三阶段对话规则执行：阶段一收集配置、阶段二生成试卷、阶段三批改评分。禁止跨阶段操作，禁止在试卷中透露答案。"}
        ]
        for msg in history[-30:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})

        llm = self.tutor.llm
        response_text = llm.chat(messages, temperature=0.5, max_tokens=4000)

        # 批改阶段自动保存记录
        if self._is_exam_grading(response_text):
            saved_path = self._save_exam_record(course_name, user_message, history, response_text)
            self._save_exam_to_memory(course_name, response_text)
            response_text += f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"

        return ChatMessage(
            role="assistant",
            content=response_text,
            citations=None,
            tool_calls=None,
        )

    def run_exam_mode_stream(
        self,
        course_name: str,
        user_message: str,
        plan: Plan,
        history: list = None,
    ):
        """对话式考试模式流式版本。"""
        if history is None:
            history = []

        context = ""
        retriever = self.load_retriever(course_name)
        if retriever:
            chunks = retriever.retrieve(user_message, top_k=12)
            context = retriever.format_context(chunks)
        else:
            context = "（未找到相关教材，请先上传课程资料）"

        prompt = EXAM_PROMPT.format(
            course_name=course_name,
            context=context,
            question=user_message,
        )

        messages = [
            {"role": "system", "content": "你是一位严肃公正的考试主考官，严格按照三阶段对话规则执行：阶段一收集配置、阶段二生成试卷、阶段三批改评分。禁止跨阶段操作，禁止在试卷中透露答案。"}
        ]
        for msg in history[-30:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": prompt})

        llm = self.tutor.llm
        collected = []
        for chunk in llm.chat_stream(messages, temperature=0.5, max_tokens=4000):
            collected.append(chunk)
            yield chunk
        full_response = "".join(collected)
        if self._is_exam_grading(full_response):
            saved_path = self._save_exam_record(course_name, user_message, history, full_response)
            self._save_exam_to_memory(course_name, full_response)
            yield f"\n\n---\n📁 **本次考试记录已保存至**：`{saved_path}`"

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
            mgr.save_episode(
                course_name=course_name,
                event_type="mistake" if signal.is_mistake else "practice",
                content=content,
                importance=0.9 if signal.is_mistake else 0.4,
                metadata={"score": signal.score, "tags": signal.mistake_tags},
            )
            if signal.mistake_tags and signal.is_mistake:
                mgr.update_weak_points(course_name, signal.mistake_tags)
            mgr.record_practice_result(course_name, signal.score, signal.is_mistake)
            print(f"[Memory] 练习{'错题' if signal.is_mistake else '结果'}已记录，得分={signal.score:.0f}")
        except Exception as _e:
            print(f"[Memory] 练习记忆写入失败（不影响评分）: {_e}")

    def _is_exam_grading(self, text: str) -> bool:
        """判断考试模式回复是否为批改阶段。"""
        keywords = ["批改报告", "逐题详批", "评分总表", "总得分", "总分", "考后建议", "薄弱知识点"]
        return sum(1 for kw in keywords if kw in text) >= 2

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

    def run(
        self,
        course_name: str,
        mode: str,
        user_message: str,
        state: Dict[str, Any] = None,
        history: List[Dict[str, str]] = None
    ) -> tuple[ChatMessage, Plan]:
        """Main orchestration entry point."""
        if history is None:
            history = []
        # Generate plan
        plan = self.router.plan(user_message, mode, course_name)
        
        # Execute based on mode
        if mode == "learn":
            response = self.run_learn_mode(course_name, user_message, plan, history)
        elif mode == "practice":
            response = self.run_practice_mode(course_name, user_message, plan, state, history)
        elif mode == "exam":
            response = self.run_exam_mode(course_name, user_message, plan, history)
        else:
            response = ChatMessage(
                role="assistant",
                content=f"未知模式: {mode}",
                citations=None,
                tool_calls=None
            )
        
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

        context = ""
        citations_dicts = []
        if plan.need_rag:
            retriever = self.load_retriever(course_name)
            if retriever:
                chunks = retriever.retrieve(user_message)
                context = retriever.format_context(chunks)
                citations_dicts = [c.model_dump() for c in chunks]
            else:
                context = "（未找到相关教材，请先上传课程资料）"

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
            mgr.save_episode(
                course_name=course_name,
                event_type="qa",
                content=content,
                importance=0.5,
                metadata={"doc_ids": doc_ids},
            )
            mgr.increment_qa_count(course_name)
        except Exception as _mem_err:
            print(f"[Memory] 写入情景记忆失败（不影响输出）: {_mem_err}")

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
        plan = self.router.plan(user_message, mode, course_name)

        if mode == "learn":
            yield from self.run_learn_mode_stream(course_name, user_message, plan, history)
        elif mode == "practice":
            yield from self.run_practice_mode_stream(course_name, user_message, plan, history)
        elif mode == "exam":
            yield from self.run_exam_mode_stream(course_name, user_message, plan, history)
        else:
            response, _ = self.run(course_name, mode, user_message, state, history)
            yield response.content
