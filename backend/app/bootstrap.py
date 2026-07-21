from __future__ import annotations

from dataclasses import dataclass

from adapters.llm import DeepSeekTutorResponder, DemoTutorResponder
from contracts.llm import TutorResponderPort
from modules.agent.service import TurnService
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker
from modules.sessions.resolver import CourseResolver
from modules.sessions.repository import SessionRepository
from modules.sessions.service import SessionService

from core.settings import Settings
from core.store import SQLiteStore


@dataclass(frozen=True)
class Application:
    settings: Settings
    store: SQLiteStore
    courses: CourseService
    knowledge: KnowledgeService
    knowledge_jobs: KnowledgeJobWorker
    sessions: SessionService
    llm: TutorResponderPort
    turns: TurnService

    def llm_health(self) -> dict[str, object]:
        status = self.llm.health()
        adapter_available = self.settings.text_provider.lower() == "deepseek"
        status.update(
            configured=self.settings.remote_llm_configured,
            enabled=self.settings.enable_remote_llm and self.settings.remote_llm_configured and adapter_available,
            adapter_available=adapter_available,
            requested_provider=self.settings.text_provider,
        )
        return status


def build_application(settings: Settings) -> Application:
    """The one composition root: modules never instantiate repositories or adapters themselves."""
    fallback = DemoTutorResponder()
    llm: TutorResponderPort = fallback
    if settings.enable_remote_llm and settings.remote_llm_configured and settings.text_provider.lower() == "deepseek":
        llm = DeepSeekTutorResponder(
            api_key=settings.text_api_key,
            base_url=settings.text_base_url,
            model=settings.text_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_output_tokens=settings.agent_max_output_tokens,
            max_retries=settings.llm_max_retries,
        )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    courses = CourseService(CourseRepository(store))
    knowledge = KnowledgeService(
        repository=KnowledgeRepository(store),
        settings=settings,
        wiki_is_enabled=lambda course_id: bool((course := courses.get_course(course_id)) and course.wiki_enabled),
    )
    resolver = CourseResolver(courses)
    sessions = SessionService(SessionRepository(store), courses, resolver)
    jobs = KnowledgeJobWorker(
        knowledge,
        workers=settings.background_job_workers,
        queue_capacity=settings.background_job_queue_capacity,
    )
    return Application(settings, store, courses, knowledge, jobs, sessions, llm, TurnService(sessions, knowledge, llm, fallback))
