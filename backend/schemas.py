"""
【模块说明】
- 主要作用：定义后端与编排层共享的数据模型（请求、响应、计划、评分信号等）。
- 核心类：CourseWorkspace、Plan、ChatRequest、TutorResult、PracticeGradeSignal。
- 典型用途：API 入参校验、Agent 间结构化数据传递、持久化前的数据标准化。
"""
from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime


class CourseWorkspace(BaseModel):
    """课程工作区配置模型。"""
    course_name: str
    subject: str  # e.g., "线性代数", "通信原理"
    created_at: datetime = Field(default_factory=datetime.now)
    documents: List[str] = Field(default_factory=list)
    index_path: Optional[str] = None
    notes_path: Optional[str] = None
    mistakes_path: Optional[str] = None
    exams_path: Optional[str] = None


class RetrievedChunk(BaseModel):
    """检索片段与引用信息模型。"""
    text: str
    doc_id: str
    page: Optional[int] = None
    chunk_id: Optional[str] = None
    score: float


class Plan(BaseModel):
    """Agent 编排计划模型。"""
    need_rag: bool = True
    allowed_tools: List[str] = Field(default_factory=list)
    task_type: Literal["learn", "practice", "exam", "general"] = "learn"
    style: Literal["step_by_step", "hint_first", "direct"] = "step_by_step"
    output_format: Literal["answer", "quiz", "exam", "report"] = "answer"


class Quiz(BaseModel):
    """练习题模型。"""
    question: str
    standard_answer: str
    rubric: str  # 评分标准说明
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    chapter: Optional[str] = None
    concept: Optional[str] = None


class GradeReport(BaseModel):
    """练习评分结果模型。"""
    score: float  # 分数范围 0-100
    feedback: str
    mistake_tags: List[str] = Field(default_factory=list)  # e.g., ["概念性错误", "计算错误"]
    references: List[RetrievedChunk] = Field(default_factory=list)


class ExamReport(BaseModel):
    """考试报告模型。"""
    overall_score: float
    weak_topics: List[str]
    recommendations: List[str]
    wrong_questions: List[Dict[str, Any]]


class ChatMessage(BaseModel):
    """对话消息模型。"""
    role: Literal["user", "assistant", "system"]
    content: str
    citations: Optional[List[RetrievedChunk]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatRequest(BaseModel):
    """对话请求模型。"""
    course_name: str
    mode: Literal["learn", "practice", "exam"]
    message: str
    history: List[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """对话响应模型。"""
    message: ChatMessage
    plan: Optional[Plan] = None


# ---------------------------------------------------------------------------
# Agent 间结构化消息类型
# ---------------------------------------------------------------------------

class ToolCallLog(BaseModel):
    """单次工具调用记录。"""
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    success: bool = True


class TutorResult(BaseModel):
    """TutorAgent 结构化输出模型。

    用于替代早期仅返回字符串的方式，避免下游依赖正则提取引用和工具调用信息。
    """
    content: str
    citations: List[RetrievedChunk] = Field(default_factory=list)
    tool_calls_log: List[ToolCallLog] = Field(default_factory=list)


class PracticeGradeSignal(BaseModel):
    """练习评分文本的结构化抽取结果。

    用于替代零散正则解析，便于在保存记忆前统一处理分数与错误标签。
    """
    score: float = 60.0
    is_mistake: bool = False
    mistake_tags: List[str] = Field(default_factory=list)
    question_summary: str = ""
    student_answer: str = ""

    @classmethod
    def from_text(
        cls,
        response_text: str,
        student_answer: str = "",
        question_summary: str = "",
    ) -> "PracticeGradeSignal":
        """从自由文本评分结果中提取分数和错误标签。"""
        import re

        score = 60.0
        m = re.search(r"得分[：:＝=]\s*([0-9]+(?:\.[0-9]+)?)", response_text)
        if m:
            score = float(m.group(1))
        else:
            m2 = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*/\s*100", response_text)
            if m2:
                score = float(m2.group(1))

        mistake_tags: List[str] = []
        tag_m = re.search(
            r"[易错提醒错误类型]{2,}[：:]\s*(.+?)(?:\n|$)", response_text
        )
        if tag_m:
            raw = tag_m.group(1).strip()
            mistake_tags = [
                t.strip()
                for t in re.split(r"[,，、；;]", raw)
                if t.strip()
            ][:5]

        return cls(
            score=score,
            is_mistake=score < 60,
            mistake_tags=mistake_tags,
            question_summary=question_summary[:300],
            student_answer=student_answer[:300],
        )
