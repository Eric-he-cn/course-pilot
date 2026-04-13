"""
【模块说明】
- 主要作用：定义后端与编排层共享的数据模型（请求、响应、计划、评分信号等）。
- 核心类：CourseWorkspace、Plan、ChatRequest、TutorResult、PracticeGradeSignal。
- 典型用途：API 入参校验、Agent 间结构化数据传递、持久化前的数据标准化。
- 阅读建议：先看模块说明，再看类/函数头部注释和关键步骤注释。
- 注释策略：每个相对独立代码块都使用“目的 + 实现方式”进行说明。
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
    question_raw: str = ""
    user_intent: str = ""
    retrieval_keywords: List[str] = Field(default_factory=list)
    retrieval_query: str = ""
    memory_query: str = ""


class PlanPlusV1(Plan):
    """v3 规划模型：在兼容 Plan 的基础上增加主 Agent 所需字段。"""

    resolved_mode: Literal["learn", "practice", "exam"] = "learn"
    mode_reason: str = ""
    need_memory: bool = True
    capabilities: List[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "medium"
    permission_mode: Literal["safe", "standard", "elevated"] = "standard"
    replan_policy: Literal["never", "once_on_failure"] = "once_on_failure"
    workflow_template: Literal[
        "learn_only",
        "practice_only",
        "exam_only",
        "learn_then_practice",
        "practice_then_review",
        "exam_then_review",
    ] = "learn_only"
    action_kind: Literal[
        "learn_explain",
        "practice_generate",
        "practice_grade",
        "exam_generate",
        "exam_grade",
        "learn_then_practice",
    ] = "learn_explain"
    route_confidence: float = 0.7
    route_reason: str = ""
    required_artifact_kind: Literal["none", "practice", "exam"] = "none"
    tool_budget: Dict[str, Any] = Field(default_factory=dict)
    allowed_tool_groups: List[str] = Field(default_factory=list)


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
    session_id: Optional[str] = None
    shadow_eval: bool = False
    history: List[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    """对话响应模型。"""
    message: ChatMessage
    plan: Optional[PlanPlusV1] = None
    session_id: Optional[str] = None
    resolved_mode: Optional[Literal["learn", "practice", "exam"]] = None
    current_stage: Optional[str] = None


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


class StatePatchV1(BaseModel):
    """Agent 对 SessionState 的增量更新。"""

    task_summary: Optional[str] = None
    current_stage: Optional[str] = None
    current_step_index: Optional[int] = None
    history_summary_state: Optional[Dict[str, Any]] = None
    selected_memory: Optional[str] = None
    last_quiz: Optional[Dict[str, Any]] = None
    last_exam: Optional[Dict[str, Any]] = None
    fallback_flags: Optional[List[str]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SessionStateV1(BaseModel):
    """服务端会话态：全局短期记忆与 Agent 通信中心。"""

    version: Literal["v1"] = "v1"
    session_id: str
    course_name: str
    requested_mode_hint: Literal["learn", "practice", "exam"] = "learn"
    resolved_mode: Literal["learn", "practice", "exam"] = "learn"
    task_full_text: str = ""
    task_summary: str = ""
    question_raw: str = ""
    user_intent: str = ""
    retrieval_query: str = ""
    memory_query: str = ""
    current_stage: str = "router_planned"
    current_step_index: int = 0
    history_summary_state: Dict[str, Any] = Field(default_factory=dict)
    selected_memory: str = ""
    last_quiz: Optional[Dict[str, Any]] = None
    last_exam: Optional[Dict[str, Any]] = None
    active_practice: Optional[Dict[str, Any]] = None
    active_exam: Optional[Dict[str, Any]] = None
    latest_submission: Optional[Dict[str, Any]] = None
    latest_grading: Optional[Dict[str, Any]] = None
    permission_mode: Literal["safe", "standard", "elevated"] = "standard"
    fallback_flags: List[str] = Field(default_factory=list)
    idempotency_keys: List[str] = Field(default_factory=list)
    tool_audit_refs: List[str] = Field(default_factory=list)
    last_taskgraph_digest: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentResultV1(BaseModel):
    """统一 Agent 输出结构，便于向 Runtime 演进。"""

    content: str = ""
    state_patch: StatePatchV1 = Field(default_factory=StatePatchV1)
    citations: List[RetrievedChunk] = Field(default_factory=list)
    tool_calls_log: List[ToolCallLog] = Field(default_factory=list)
    diagnostics: Dict[str, Any] = Field(default_factory=dict)


class AgentContextV1(BaseModel):
    """Specialist Agent 的只读上下文快照。"""

    version: Literal["v1"] = "v1"
    session_snapshot: SessionStateV1
    history_context: str = ""
    rag_context: str = ""
    memory_context: str = ""
    merged_context: str = ""
    citations: List[RetrievedChunk] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    tool_scope: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ArtifactQuestionV1(BaseModel):
    """统一题目条目：practice/exam 共用。"""

    id: int
    type: str
    question: str
    options: List[str] = Field(default_factory=list)
    score: int = 0
    standard_answer: str
    rubric: str
    chapter: str = ""
    concept: str = ""
    difficulty: str = "medium"


class PracticeArtifactV1(BaseModel):
    """结构化练习 artifact。"""

    kind: Literal["practice"] = "practice"
    title: str = "练习题"
    instructions: str = "请回答上述题目，回答完毕后我会为你评分并给出详细讲解。"
    topic: str = ""
    requested_num_questions: int = 1
    question_type: str = "综合题"
    questions: List[ArtifactQuestionV1] = Field(default_factory=list)
    total_score: int = 100


class ExamArtifactV1(BaseModel):
    """结构化考试 artifact。"""

    kind: Literal["exam"] = "exam"
    title: str
    instructions: str
    questions: List[ArtifactQuestionV1] = Field(default_factory=list)
    total_score: int = 100


class SubmissionArtifactV1(BaseModel):
    """用户提交的结构化描述。"""

    source_message: str
    artifact_kind: Literal["practice", "exam"]
    session_id: str = ""


class GradingArtifactV1(BaseModel):
    """评分结果的结构化描述。"""

    artifact_kind: Literal["practice", "exam"]
    report_text: str
    overall_score: Optional[float] = None
    weak_topics: List[str] = Field(default_factory=list)
    mistake_tags: List[str] = Field(default_factory=list)


class PrefetchBundleV1(BaseModel):
    """Runtime 预取后的候选上下文材料。"""

    version: Literal["v1"] = "v1"
    session_snapshot: SessionStateV1
    candidate_history_context: str = ""
    candidate_rag_context: str = ""
    candidate_memory_context: str = ""
    candidate_merged_context: str = ""
    citations: List[RetrievedChunk] = Field(default_factory=list)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    tool_scope: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ToolDecision(BaseModel):
    """ToolHub 的统一门控结果。"""

    tool_name: str
    allowed: bool
    reason: str
    signature: str
    permission_mode: Literal["safe", "standard", "elevated"] = "standard"
    idempotency_key: str = ""
    dedup_hit: bool = False
    dedup_reason: str = ""


class ToolAuditRecord(BaseModel):
    """工具调用审计记录。"""

    tool_name: str
    signature: str
    permission_mode: Literal["safe", "standard", "elevated"] = "standard"
    allowed: bool = True
    reason: str = "allowed"
    success: bool = False
    dedup_hit: bool = False
    dedup_reason: str = ""
    idempotency_key: str = ""
    failure_class: str = ""
    via: str = ""
    elapsed_ms: float = 0.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


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
