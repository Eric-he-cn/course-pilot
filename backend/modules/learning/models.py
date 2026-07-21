from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class EvidenceEvent:
    id: str; course_id: str; concept_id: str | None; attribution_status: str; topic_hint: str | None; kind: str; created_at: str
@dataclass(frozen=True)
class ArchiveSummary:
    course_id: str; evidence_count: int; events: list[EvidenceEvent]
