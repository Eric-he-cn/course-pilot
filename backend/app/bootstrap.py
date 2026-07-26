from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from adapters.embedding import BgeEmbedder
from adapters.llm import DeepSeekAgentChat, DemoAgentChat, QwenOcrTranscriber
from contracts.llm import AgentChatPort, VisionTranscriberPort
from modules.agent.service import TurnService
from modules.agent.skills import SkillRegistry, UserSkillStore
from modules.agent.trace import TraceWriter
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker
from modules.learning.repository import LearningRepository
from modules.learning.service import LearningService
from modules.memory.store import MemoryStore
from modules.planning.repository import PlanningRepository
from modules.planning.service import PlanningService
from modules.sessions.resolver import CourseResolver
from modules.sessions.artifacts import ArtifactStore
from modules.sessions.compactions import CompactionStore
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
    llm: AgentChatPort
    turns: TurnService
    learning: LearningService
    planning: PlanningService
    skills: SkillRegistry
    vision: VisionTranscriberPort | None = None

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

    def vision_health(self) -> dict[str, object]:
        if self.vision is None:
            return {
                "configured": self.settings.vision_configured,
                "enabled": False,
                "requested_provider": self.settings.vision_provider,
            }
        return {**self.vision.health(), "requested_provider": self.settings.vision_provider}


def build_application(settings: Settings) -> Application:
    """The one composition root: modules never instantiate repositories or adapters themselves."""
    fallback = DemoAgentChat()
    llm: AgentChatPort = fallback
    if settings.enable_remote_llm and settings.remote_llm_configured and settings.text_provider.lower() == "deepseek":
        llm = DeepSeekAgentChat(
            api_key=settings.text_api_key,
            base_url=settings.text_base_url,
            model=settings.text_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_output_tokens=settings.agent_max_output_tokens,
            max_retries=settings.llm_max_retries,
        )
    vision: VisionTranscriberPort | None = None
    if settings.enable_remote_llm and settings.vision_configured and settings.vision_provider.lower() == "dashscope":
        vision = QwenOcrTranscriber(
            api_key=settings.vision_api_key,
            base_url=settings.vision_base_url,
            model=settings.vision_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    store = SQLiteStore(settings.database_path)
    store.migrate()
    courses = CourseService(CourseRepository(store))
    embedder = (
        BgeEmbedder(model_name=settings.rag_embedding_model, device=settings.rag_embedding_device, batch_size=settings.rag_embedding_batch_size)
        if settings.rag_embedding_model
        else None
    )
    knowledge = KnowledgeService(
        repository=KnowledgeRepository(store),
        settings=settings,
        wiki_is_enabled=lambda course_id: bool((course := courses.get_course(course_id)) and course.wiki_enabled),
        embedder=embedder,
    )
    resolver = CourseResolver(courses)
    sessions = SessionService(
        SessionRepository(store), courses, resolver,
        vision=vision,
        attachment_max_bytes=settings.attachment_max_bytes,
        attachment_max_pixels=settings.attachment_max_pixels,
    )
    jobs = KnowledgeJobWorker(
        knowledge,
        workers=settings.background_job_workers,
        queue_capacity=settings.background_job_queue_capacity,
    )
    learning = LearningService(LearningRepository(store))
    planning = PlanningService(PlanningRepository(store), concept_exists=knowledge.concept_exists)
    # 内建 skill 目录随代码走（架构 §6）；导入的 skill 存库，启用后并入同一注册表。
    skills = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin", user_skills=UserSkillStore(store))
    turns = TurnService(
        sessions, knowledge, planning, learning, llm, fallback,
        plan_writer=planning, evidence=learning, artifacts=ArtifactStore(store), compactions=CompactionStore(store), skills=skills, memory=MemoryStore(settings.data_dir),
        trace=TraceWriter(settings.data_dir / "traces"),
        history_token_budget=settings.agent_history_token_budget,
        context_char_limit=settings.agent_context_char_limit,
        compact_threshold_ratio=settings.agent_compact_threshold_ratio,
    )
    return Application(settings, store, courses, knowledge, jobs, sessions, llm, turns, learning, planning, skills, vision)
