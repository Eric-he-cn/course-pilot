from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from adapters.embedding import BgeEmbedder
from adapters.web import HttpWebAccess
from adapters.llm import DemoAgentChat, OpenAICompatibleChat, VisionOcrTranscriber
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
        # 适配器认 OpenAI 兼容协议，配齐三项就能用，不挑厂商。
        choices = self.settings.models
        status.update(
            configured=self.settings.remote_llm_configured,
            enabled=self.settings.enable_remote_llm and self.settings.remote_llm_configured,
            adapter_available=True,
            requested_provider=self.settings.text_provider,
            # 界面据此渲染切换项；只列真的配齐了的。
            choices=[
                {"key": choice.key, "label": choice.label, "model": choice.model,
                 "provider": choice.provider, "thinking_default": choice.thinking_default}
                for choice in choices if choice.configured
            ],
            default_choice=choices[0].key,
            default_thinking=choices[0].thinking_default,
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


def _with_thinking(extra_body: dict[str, object], enabled: bool) -> dict[str, object]:
    """思考开关是厂商私有字段。用户没切换过就用配置里的原样值；切换了才覆盖。"""
    return {**extra_body, "thinking": {"type": "enabled" if enabled else "disabled"}}


@dataclass(frozen=True)
class SharedRuntime:
    """跨用户共享的部分：适配器与向量模型只建一次。"""
    llm: AgentChatPort
    fallback: AgentChatPort
    classifier: AgentChatPort | None
    vision: VisionTranscriberPort | None
    web: WebSearchPort | None
    embedder: object | None
    # (模型 key, 是否开思考) → 适配器。空表示没配远端，一律走 fallback。
    responders: dict[tuple[str, bool], AgentChatPort] = field(default_factory=dict)

    def close(self) -> None:
        for item in (self.llm, self.fallback, self.classifier, self.vision, *self.responders.values()):
            close = getattr(item, "close", None)
            if callable(close):
                try: close()
                except Exception: pass


def build_shared_runtime(settings: Settings) -> SharedRuntime:
    fallback = DemoAgentChat()
    llm: AgentChatPort = fallback
    classifier: AgentChatPort | None = None
    responders: dict[tuple[str, bool], AgentChatPort] = {}
    # 认的是「配了 OpenAI 兼容端点」而不是某个厂商名：写死厂商名会让别家的配置静默退回本地兜底。
    remote_ready = settings.enable_remote_llm and settings.remote_llm_configured
    if remote_ready:
        # 每个模型建开、关思考两个实例。思考是每次请求的选项，但把它做成 chat() 的参数要动
        # 协议的全部实现；适配器本身很轻，多一个实例便宜得多。
        for choice in settings.models:
            if not choice.configured:
                continue
            for thinking in (True, False):
                responders[(choice.key, thinking)] = OpenAICompatibleChat(
                    api_key=choice.api_key, base_url=choice.base_url, model=choice.model,
                    provider=choice.provider, extra_body=_with_thinking(choice.extra_body, thinking),
                    connect_timeout_seconds=settings.llm_connect_timeout_seconds,
                    total_timeout_seconds=settings.llm_total_timeout_seconds,
                    max_output_tokens=settings.agent_max_output_tokens,
                    max_retries=settings.llm_max_retries,
                )
        first = settings.models[0]
        llm = responders.get((first.key, first.thinking_default), fallback)
        # 学科分类器：超时更短、不重试，它跑在 turn 锁内、首个增量之前，所以固定用第一个模型
        # 并关掉思考——它的任务只是从清单里挑一个 id。
        classifier = OpenAICompatibleChat(
            api_key=first.api_key, base_url=first.base_url, model=first.model,
            provider=first.provider, extra_body=_with_thinking(first.extra_body, False),
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=6, max_output_tokens=256, max_retries=0,
        )
    vision: VisionTranscriberPort | None = None
    if settings.enable_remote_llm and settings.vision_configured:
        vision = VisionOcrTranscriber(
            api_key=settings.vision_api_key, base_url=settings.vision_base_url, model=settings.vision_model,
            provider=settings.vision_provider,
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
    return SharedRuntime(llm=llm, fallback=fallback, classifier=classifier, vision=vision, web=web, embedder=embedder, responders=responders)


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
    choices = settings.models
    default_key, default_thinking = choices[0].key, choices[0].thinking_default

    def select_responder(key: str | None, thinking: bool | None) -> AgentChatPort:
        """认不出的模型 key 一律落回第一个，别让一个过期的前端选择把整轮打挂。"""
        if not runtime.responders:
            return llm
        wanted = key if any(model.key == key for model in choices) else default_key
        return runtime.responders.get((wanted, default_thinking if thinking is None else thinking), llm)

    turns = TurnService(
        sessions, knowledge, planning, learning, llm, fallback,
        select_responder=select_responder,
        plan_writer=planning, evidence=learning, artifacts=ArtifactStore(store), compactions=CompactionStore(store), skills=skills, memory=memory,
        web=web, notes=notes,
        trace=TraceWriter(settings.data_dir / "traces"),
        history_token_budget=settings.agent_history_token_budget,
        context_char_limit=settings.agent_context_char_limit,
        compact_threshold_ratio=settings.agent_compact_threshold_ratio,
    )
    return Application(settings, store, courses, knowledge, jobs, sessions, llm, turns, learning, planning, skills, notes, memory, vision, web)
