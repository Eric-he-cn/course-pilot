from __future__ import annotations
from .models import ArchiveSummary, EvidenceEvent
from .repository import LearningRepository
class LearningService:
    """学习档案的只读骨架：证据事件表已就位，写入链路随掌握度功能落地。"""
    def __init__(self, repository: LearningRepository) -> None: self._repository = repository
    def get_archive(self, *, course_id: str, limit: int = 20) -> ArchiveSummary:
        events = [
            EvidenceEvent(row["id"], row["course_id"], row["concept_id"], row["attribution_status"], row["topic_hint"], row["kind"], row["created_at"])
            for row in self._repository.list_recent_event_rows(course_id=course_id, limit=limit)
        ]
        return ArchiveSummary(course_id=course_id, evidence_count=self._repository.count_events(course_id=course_id), events=events)
