"""TaskGraph models for the v3 execution runtime."""

from __future__ import annotations

from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


TaskStepName = Literal[
    "plan_intent",
    "prefetch_rag",
    "prefetch_memory",
    "build_agent_context",
    "detect_submission",
    "run_tutor",
    "run_quiz",
    "run_exam",
    "run_grade",
    "persist_session_state",
    "persist_records",
    "persist_memory",
    "synthesize_final",
]


class TaskGraphStepV1(BaseModel):
    """One executable or declarative step in the runtime graph."""

    step_name: TaskStepName
    phase: Literal["plan", "prefetch", "context", "route", "execute", "persist", "finalize"]
    status: Literal["pending", "skipped", "completed"] = "pending"
    side_effect: bool = False
    stream: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskGraphV1(BaseModel):
    """Compiled task graph for a single request."""

    version: Literal["v1"] = "v1"
    graph_id: str
    session_id: str
    mode_hint: Literal["learn", "practice", "exam"]
    resolved_mode: Literal["learn", "practice", "exam"]
    workflow_template: Literal[
        "learn_only",
        "practice_only",
        "exam_only",
        "learn_then_practice",
        "practice_then_review",
        "exam_then_review",
    ]
    action_kind: Literal[
        "learn_explain",
        "practice_generate",
        "practice_grade",
        "exam_generate",
        "exam_grade",
        "learn_then_practice",
    ]
    route: Literal["run_tutor", "run_quiz", "run_exam", "run_grade"]
    steps: List[TaskGraphStepV1] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def step_names(self) -> List[str]:
        return [step.step_name for step in self.steps]
