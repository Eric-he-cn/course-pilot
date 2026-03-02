"""Backend schemas for Course Learning Agent."""
from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime


class CourseWorkspace(BaseModel):
    """Course workspace configuration."""
    course_name: str
    subject: str  # e.g., "线性代数", "通信原理"
    created_at: datetime = Field(default_factory=datetime.now)
    documents: List[str] = Field(default_factory=list)
    index_path: Optional[str] = None
    notes_path: Optional[str] = None
    mistakes_path: Optional[str] = None
    exams_path: Optional[str] = None


class RetrievedChunk(BaseModel):
    """Retrieved document chunk with citation."""
    text: str
    doc_id: str
    page: Optional[int] = None
    chunk_id: Optional[str] = None
    score: float


class Plan(BaseModel):
    """Agent orchestration plan."""
    need_rag: bool = True
    allowed_tools: List[str] = Field(default_factory=list)
    task_type: Literal["learn", "practice", "exam", "general"] = "learn"
    style: Literal["step_by_step", "hint_first", "direct"] = "step_by_step"
    output_format: Literal["answer", "quiz", "exam", "report"] = "answer"


class Quiz(BaseModel):
    """Quiz question."""
    question: str
    standard_answer: str
    rubric: str  # Evaluation criteria
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    chapter: Optional[str] = None
    concept: Optional[str] = None


class GradeReport(BaseModel):
    """Grading report for practice."""
    score: float  # 0-100
    feedback: str
    mistake_tags: List[str] = Field(default_factory=list)  # e.g., ["概念性错误", "计算错误"]
    references: List[RetrievedChunk] = Field(default_factory=list)


class ExamReport(BaseModel):
    """Exam performance report."""
    overall_score: float
    weak_topics: List[str]
    recommendations: List[str]
    wrong_questions: List[Dict[str, Any]]


class ChatMessage(BaseModel):
    """Chat message."""
    role: Literal["user", "assistant", "system"]
    content: str
    citations: Optional[List[RetrievedChunk]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatRequest(BaseModel):
    """Chat request."""
    course_name: str
    mode: Literal["learn", "practice", "exam"]
    message: str
    history: List[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """Chat response."""
    message: ChatMessage
    plan: Optional[Plan] = None


# ---------------------------------------------------------------------------
# Structured inter-agent message types
# ---------------------------------------------------------------------------

class ToolCallLog(BaseModel):
    """Single tool invocation record."""
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    success: bool = True


class TutorResult(BaseModel):
    """Structured output from TutorAgent.teach().

    Replaces the raw ``str`` return so downstream runner code can access
    citations and tool call logs without regex parsing.
    """
    content: str
    citations: List[RetrievedChunk] = Field(default_factory=list)
    tool_calls_log: List[ToolCallLog] = Field(default_factory=list)


class PracticeGradeSignal(BaseModel):
    """Structured signal extracted from an inline practice-grading response.

    Replaces ad-hoc regex inside ``_save_grading_to_memory`` so the
    extraction logic is centralised and testable.
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
        """Parse score and mistake tags from a free-text grading response."""
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
