from __future__ import annotations
from dataclasses import dataclass, field
@dataclass(frozen=True)
class EvidenceEvent:
    id: str; course_id: str; concept_id: str | None; attribution_status: str; topic_hint: str | None; kind: str; created_at: str
    concept_name: str | None = None  # 界面展示用；概念 id 对用户没有意义
@dataclass(frozen=True)
class ConceptMastery:
    """展示用的掌握度快照；score 为 None 表示可归因的客观证据还不够。"""
    concept_id: str; name: str; score: float | None; objective_events: int
    due_at: str | None; insufficient_evidence: bool; algorithm_version: str
@dataclass(frozen=True)
class ArchiveSummary:
    course_id: str; evidence_count: int; events: list[EvidenceEvent]
    mastery: list[ConceptMastery] = field(default_factory=list)
    unattributed: list[dict] = field(default_factory=list)
