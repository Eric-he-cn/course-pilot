from __future__ import annotations
from datetime import datetime, timezone
from core.common import new_id, utc_now
from .mastery import ALGORITHM_VERSION, ALL_KINDS, MIN_OBJECTIVE_EVENTS, OBJECTIVE_KINDS, mastery_score, replay
from .models import ArchiveSummary, ConceptMastery, EvidenceEvent
from .repository import LearningRepository
class LearningService:
    """学习档案：证据事件是唯一真源，掌握度投影随时可从事件流重建。"""
    def __init__(self, repository: LearningRepository) -> None: self._repository = repository
    def get_archive(self, *, course_id: str, limit: int = 20) -> ArchiveSummary:
        events = [
            EvidenceEvent(row["id"], row["course_id"], row["concept_id"], row["attribution_status"], row["topic_hint"], row["kind"], row["created_at"], row["concept_name"])
            for row in self._repository.list_recent_event_rows(course_id=course_id, limit=limit)
        ]
        return ArchiveSummary(
            course_id=course_id, evidence_count=self._repository.count_events(course_id=course_id), events=events,
            mastery=self.mastery(course_id=course_id), unattributed=self._repository.unattributed_rows(course_id=course_id),
        )

    def record_evidence(self, *, course_id: str, kind: str, concept_id: str | None = None, topic_hint: str | None = None, payload: dict | None = None) -> EvidenceEvent:
        """写一条证据事件并同步刷新该概念的投影。

        概念必须存在于本课程的概念目录里，否则记为 unattributed——这是挡住幻觉
        概念污染学习档案的服务端闸门（架构 §7.5 / §8.1）。
        """
        if kind not in ALL_KINDS:
            raise ValueError(f"未知的证据类型 {kind}；可用：{'、'.join(sorted(ALL_KINDS))}")
        resolved_id, status = None, "unattributed"
        if concept_id:
            row = self._repository.concept_row(concept_id)
            if row is not None and row["course_id"] == course_id:
                resolved_id, status = row["id"], "attributed"
            elif not topic_hint:
                topic_hint = concept_id[:80]  # 保留模型的原始说法，供管理页补录
        elif not topic_hint:
            raise ValueError("无法归因的证据必须带 topic_hint")
        timestamp, event_id = utc_now(), new_id("evidence")
        self._repository.insert_event(
            event_id=event_id, course_id=course_id, concept_id=resolved_id, attribution_status=status,
            topic_hint=None if resolved_id else (topic_hint or None), kind=kind, payload=payload or {}, timestamp=timestamp,
        )
        if resolved_id and kind in OBJECTIVE_KINDS:
            self._project(concept_id=resolved_id, course_id=course_id)
        return EvidenceEvent(event_id, course_id, resolved_id, status, None if resolved_id else topic_hint, kind, timestamp)

    def _project(self, *, concept_id: str, course_id: str) -> None:
        state = replay(self._repository.concept_event_rows(concept_id))
        self._repository.upsert_mastery(
            concept_id=concept_id, course_id=course_id, bkt_p=state.bkt_p, stability=state.stability,
            difficulty=state.difficulty, objective_events=state.objective_events,
            last_reviewed_at=state.last_reviewed_at, due_at=state.due_at,
            algorithm_version=ALGORITHM_VERSION, timestamp=utc_now(),
        )

    def rebuild(self, *, course_id: str) -> int:
        """算法或参数变更后从事件流全量重建投影（架构 §11）。"""
        concept_ids = {row["concept_id"] for row in self._repository.list_recent_event_rows(course_id=course_id, limit=10_000) if row["concept_id"]}
        for concept_id in concept_ids:
            self._project(concept_id=concept_id, course_id=course_id)
        return len(concept_ids)

    def mastery(self, *, course_id: str, limit: int = 60) -> list[ConceptMastery]:
        now = datetime.now(timezone.utc)
        result = []
        for row in self._repository.mastery_rows(course_id=course_id, limit=limit):
            score = mastery_score(replay(self._repository.concept_event_rows(row["concept_id"])), at=now)
            result.append(ConceptMastery(
                concept_id=row["concept_id"], name=row["name"], score=score,
                objective_events=row["objective_events"], due_at=row["due_at"],
                insufficient_evidence=score is None, algorithm_version=row["algorithm_version"],
            ))
        return result

    def weak_concepts(self, *, course_id: str, limit: int = 5) -> list[ConceptMastery]:
        """弱项：证据足够且分数最低的概念，供练习出题时优先覆盖。"""
        scored = [item for item in self.mastery(course_id=course_id) if item.score is not None]
        return sorted(scored, key=lambda item: item.score or 0.0)[:limit]

    def due_concepts(self, *, course_id: str, limit: int = 5) -> list[ConceptMastery]:
        """按 FSRS 排期已到期的概念，供排计划时优先安排复习。"""
        now = utc_now()
        due = [item for item in self.mastery(course_id=course_id) if item.due_at and item.due_at <= now]
        return sorted(due, key=lambda item: item.due_at or "")[:limit]

    @staticmethod
    def min_objective_events() -> int:
        return MIN_OBJECTIVE_EVENTS
