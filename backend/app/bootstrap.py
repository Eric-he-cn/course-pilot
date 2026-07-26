from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from adapters.embedding import BgeEmbedder
from adapters.web import HttpWebAccess
from adapters.llm import DeepSeekAgentChat, DemoAgentChat, QwenOcrTranscriber
from contracts.llm import AgentChatPort, VisionTranscriberPort
from contracts.web import WebSearchPort
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
from modules.notes.store import NoteStore
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
    notes: NoteStore
    memory: MemoryStore
    vision: VisionTranscriberPort | None = None
    web: WebSearchPort | None = None

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

    def web_health(self) -> dict[str, object]:
        if self.web is None:
            return {"configured": self.settings.web_search_configured, "enabled": False, "provider": "serpapi"}
        return {**self.web.health(), "enabled": True}

    def vision_health(self) -> dict[str, object]:
        if self.vision is None:
            return {
                "configured": self.settings.vision_configured,
                "enabled": False,
                "requested_provider": self.settings.vision_provider,
            }
        return {**self.vision.health(), "requested_provider": self.settings.vision_provider}


@dataclass(frozen=True)
class SharedRuntime:
    """跨用户共享的部分：适配器与向量模型只建一次。"""
    llm: AgentChatPort
    fallback: AgentChatPort
    classifier: AgentChatPort | None
    vision: VisionTranscriberPort | None
    web: WebSearchPort | None
    embedder: object | None

    def close(self) -> None:
        for item in (self.llm, self.fallback, self.classifier, self.vision):
            close = getattr(item, "close", None)
            if callable(close):
                try: close()
                except Exception: pass


def build_shared_runtime(settings: Settings) -> SharedRuntime:
    fallback = DemoAgentChat()
    llm: AgentChatPort = fallback
    if settings.enable_remote_llm and settings.remote_llm_configured and settings.text_provider.lower() == "deepseek":
        llm = DeepSeekAgentChat(
            api_key=settings.text_api_key, base_url=settings.text_base_url, model=settings.text_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_output_tokens=settings.agent_max_output_tokens,
            max_retries=settings.llm_max_retries,
        )
    # 学科分类器：超时更短、不重试，它跑在 turn 锁内、首个增量之前。
    classifier: AgentChatPort | None = None
    if settings.enable_remote_llm and settings.remote_llm_configured and settings.text_provider.lower() == "deepseek":
        classifier = DeepSeekAgentChat(
            api_key=settings.text_api_key, base_url=settings.text_base_url, model=settings.text_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=6, max_output_tokens=256, max_retries=0,
        )
    vision: VisionTranscriberPort | None = None
    if settings.enable_remote_llm and settings.vision_configured and settings.vision_provider.lower() == "dashscope":
        vision = QwenOcrTranscriber(
            api_key=settings.vision_api_key, base_url=settings.vision_base_url, model=settings.vision_model,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.llm_total_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    web: WebSearchPort | None = None
    if settings.enable_remote_llm and settings.web_search_configured:
        web = HttpWebAccess(
            api_key=settings.web_search_api_key,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.web_timeout_seconds,
        )
    embedder = (
        BgeEmbedder(model_name=settings.rag_embedding_model, device=settings.rag_embedding_device, batch_size=settings.rag_embedding_batch_size)
        if settings.rag_embedding_model else None
    )
    return SharedRuntime(llm=llm, fallback=fallback, classifier=classifier, vision=vision, web=web, embedder=embedder)


def build_application(settings: Settings, shared: SharedRuntime | None = None) -> Application:
    """The one composition root: modules never instantiate repositories or adapters themselves."""
    runtime = shared or build_shared_runtime(settings)
    llm, fallback, classifier = runtime.llm, runtime.fallback, runtime.classifier
    vision, web, embedder = runtime.vision, runtime.web, runtime.embedder
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(settings.database_path)
    store.migrate()
    courses = CourseService(CourseRepository(store))
    knowledge = KnowledgeService(
        repository=KnowledgeRepository(store),
        settings=settings,
        wiki_is_enabled=lambda course_id: bool((course := courses.get_course(course_id)) and course.wiki_enabled),
        embedder=embedder,
    )
    resolver = CourseResolver(courses, classifier=classifier)
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
    notes = NoteStore(settings.data_dir)
    memory = MemoryStore(settings.data_dir)
    learning = LearningService(LearningRepository(store))
    planning = PlanningService(PlanningRepository(store), concept_exists=knowledge.concept_exists)
    # 内建 skill 目录随代码走（架构 §6）；导入的 skill 存库，启用后并入同一注册表。
    skills = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin", user_skills=UserSkillStore(store))
    turns = TurnService(
        sessions, knowledge, planning, learning, llm, fallback,
        plan_writer=planning, evidence=learning, artifacts=ArtifactStore(store), compactions=CompactionStore(store), skills=skills, memory=memory,
        web=web, notes=notes,
        trace=TraceWriter(settings.data_dir / "traces"),
        history_token_budget=settings.agent_history_token_budget,
        context_char_limit=settings.agent_context_char_limit,
        compact_threshold_ratio=settings.agent_compact_threshold_ratio,
    )
    return Application(settings, store, courses, knowledge, jobs, sessions, llm, turns, learning, planning, skills, notes, memory, vision, web)
