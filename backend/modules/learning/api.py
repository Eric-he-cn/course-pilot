from __future__ import annotations
from typing import Protocol
from .mistakes import GRADUATE_STREAK  # 毕业阈值要写进给模型看的档案文本，跟着 Port 一起出口
from .models import ArchiveSummary, ConceptMastery, EvidenceEvent, MistakeRecord

__all__ = ["GRADUATE_STREAK", "ArchiveReaderPort", "ArchiveSummary", "ConceptMastery",
           "EvidenceEvent", "EvidenceWriterPort", "MistakeRecord"]


class ArchiveReaderPort(Protocol):
    def get_archive(self, *, course_id: str, limit: int = 20, mistake_limit: int = 20) -> ArchiveSummary: ...
    def weak_concepts(self, *, course_id: str, limit: int = 5) -> list[ConceptMastery]: ...
    def due_concepts(self, *, course_id: str, limit: int = 5) -> list[ConceptMastery]: ...


class EvidenceWriterPort(Protocol):
    """写入侧单独成 port：只有 emit_evidence 工具用到它。"""
    def record_evidence(self, *, course_id: str, kind: str, concept_id: str | None = None, topic_hint: str | None = None, payload: dict | None = None) -> EvidenceEvent: ...
