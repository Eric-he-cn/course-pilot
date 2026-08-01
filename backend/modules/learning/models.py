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
class MistakeRecord:
    """错题库里的一条，粒度是概念；streak 是当前连对次数，答错归零。"""
    concept_id: str; name: str; status: str; wrong_count: int; streak: int
    first_wrong_at: str; last_wrong_at: str; graduated_at: str | None; relapse_count: int
@dataclass(frozen=True)
class ArchiveSummary:
    course_id: str; evidence_count: int; events: list[EvidenceEvent]
    mastery: list[ConceptMastery] = field(default_factory=list)
    unattributed: list[dict] = field(default_factory=list)
    # mistakes 是一页（活跃优先），不是全量；两个计数才是总数。
    mistakes: list[MistakeRecord] = field(default_factory=list)
    active_count: int = 0
    graduated_count: int = 0
    # 连对几次算清掉。随响应下发，界面不必自己存一份——两份常量不同步时界面会照旧
    # 画「连对 M/2」的进度条，说的却是假话，而这种错没有任何门能发现。
    graduate_streak: int = 0
