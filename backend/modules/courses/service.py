from __future__ import annotations
from app.common import new_id, utc_now
from .models import Course
from .repository import CourseRepository
_COLORS = ("#B56E3D", "#176B5B", "#365F91", "#8C5B96", "#9A650D", "#B23A36")
class CourseService:
    def __init__(self, repository: CourseRepository) -> None: self._repository = repository
    @staticmethod
    def _course(row) -> Course: return Course(row["id"], row["name"], row["color"], bool(row["wiki_enabled"]), row["created_at"], row["updated_at"])
    def list_courses(self) -> list[Course]: return [self._course(row) for row in self._repository.list_rows()]
    def get_course(self, course_id: str) -> Course | None:
        row = self._repository.get_row(course_id); return self._course(row) if row else None
    def create_course(self, *, name: str, color: str | None = None) -> Course:
        clean_name = name.strip()
        if not clean_name: raise ValueError("课程名称不能为空")
        if len(clean_name) > 100: raise ValueError("课程名称不能超过 100 个字符")
        chosen = color or _COLORS[len(self.list_courses()) % len(_COLORS)]
        if not chosen.startswith("#") or len(chosen) != 7: raise ValueError("课程颜色必须是 #RRGGBB")
        course_id, timestamp = new_id("course"), utc_now(); self._repository.insert(course_id=course_id, name=clean_name, color=chosen, timestamp=timestamp); return self.get_course(course_id)  # type: ignore[return-value]
    def update_course(self, course_id: str, *, wiki_enabled: bool | None = None, name: str | None = None) -> Course | None:
        if not self.get_course(course_id): return None
        if name is not None and not name.strip(): raise ValueError("课程名称不能为空")
        self._repository.update(course_id, name=name.strip() if name is not None else None, wiki_enabled=wiki_enabled, timestamp=utc_now()); return self.get_course(course_id)
