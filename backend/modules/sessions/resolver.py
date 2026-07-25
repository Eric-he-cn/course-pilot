from __future__ import annotations
from modules.courses.api import CourseCatalogPort
from .models import ResolvedCourseContext, SessionSummary
class CourseResolver:
    version = "course_resolver_v1"
    def __init__(self, courses: CourseCatalogPort) -> None: self._courses = courses
    def resolve(self, *, turn_id: str, session: SessionSummary, message: str) -> ResolvedCourseContext:
        if session.scope_mode == "course" and session.course_id:
            course = self._courses.get_course(session.course_id); return ResolvedCourseContext(turn_id, "resolved", session.course_id, course.name if course else None, course.color if course else None, "course_session", self.version)
        normalized = message.casefold(); candidates = [course for course in self._courses.list_courses() if course.name.casefold() in normalized]
        if len(candidates) > 1:
            # 课程名互相包含时（"深度学习" 与 "深度学习进阶"）取更具体的那个；名字互不包含才是真歧义。
            candidates = [course for course in candidates if not any(other.id != course.id and course.name.casefold() in other.name.casefold() for other in candidates)]
        if len(candidates) == 1:
            course = candidates[0]; return ResolvedCourseContext(turn_id, "resolved", course.id, course.name, course.color, "explicit_course_name", self.version)
        if len(candidates) > 1: return ResolvedCourseContext(turn_id, "ambiguous", None, None, None, "multiple_course_names", self.version, tuple(course.name for course in candidates))
        if session.resolved_course_id:
            # 通用会话沿用最近一次可靠解析，用户追问不必每轮重复课程名。
            course = self._courses.get_course(session.resolved_course_id)
            if course: return ResolvedCourseContext(turn_id, "resolved", course.id, course.name, course.color, "recent_resolution", self.version)
        courses = self._courses.list_courses()
        if len(courses) == 1:
            course = courses[0]; return ResolvedCourseContext(turn_id, "resolved", course.id, course.name, course.color, "only_available_course", self.version)
        return ResolvedCourseContext(turn_id, "unresolved", None, None, None, "course_not_identified", self.version)
