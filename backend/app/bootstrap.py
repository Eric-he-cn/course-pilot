from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from adapters.cloud_retrieval import CloudEmbedder, CloudReranker
from adapters.embedding import BgeEmbedder
from adapters.mcp_http import StreamableHttpTransport
from adapters.reranker import CrossEncoderReranker
from adapters.web import HttpWebAccess
from adapters.llm import DemoAgentChat, OpenAICompatibleChat, ResponsesApiChat, VisionOcrTranscriber
from contracts.llm import AgentChatPort, VisionTranscriberPort
from contracts.web import WebSearchPort
from modules.agent.service import TurnService
from modules.agent.skills import SkillRegistry, UserSkillStore
from modules.agent.tools import validate_profiles
from modules.agent.trace import TraceWriter
from modules.courses.repository import CourseRepository
from modules.courses.service import CourseService
from modules.knowledge.repository import KnowledgeRepository
from modules.knowledge.wiki import WikiStore
from modules.knowledge.service import KnowledgeService
from modules.knowledge.worker import KnowledgeJobWorker
from modules.learning.repository import LearningRepository
from modules.learning.service import LearningService
from modules.mcp.repository import McpRepository
from modules.mcp.service import McpService
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

logger = logging.getLogger(__name__)


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
    mcp: McpService
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
                 "provider": choice.provider, "thinking_default": choice.thinking_tier}
                for choice in choices if choice.configured
            ],
            default_choice=choices[0].key,
            default_thinking=choices[0].thinking_tier,
            thinking_tiers=list(THINKING_TIERS),
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


# 思考档位 → 请求字段。这里写的是 DeepSeek V4 的形态：thinking.type 只接受
# adaptive / enabled / disabled（不合法的值服务端会把合法列表回给你），思考深度是
# 另一个维度，由 thinking.effort 表达。
#
# 换成别家模型时只改这张表，档位 key 不要动——前端下拉与请求头都按 key 传。参考：
#   OpenAI o 系列：顶层 {"reasoning_effort": "low" | "medium" | "high"}
#   Anthropic：    {"thinking": {"type": "enabled", "budget_tokens": 8000}}
# 不支持思考的模型把四个档位都映射成 {} 即可，界面照旧能切、只是没有效果。
THINKING_TIERS: dict[str, dict[str, object]] = {
    "off": {"thinking": {"type": "disabled"}},
    "adaptive": {"thinking": {"type": "adaptive"}},
    "high": {"thinking": {"type": "enabled", "effort": "high"}},
    "max": {"thinking": {"type": "enabled", "effort": "max"}},
}
# Responses 协议下同一档位换 reasoning.effort 表达——上面那个 thinking 字段在这条协议里
# 会被静默忽略，不换表的话界面上的思考开关就成了摆设。真机实测 DeepSeek 接受的取值是
# none / minimal / low / medium / high / xhigh / max；adaptive 没有对应值，整个字段不发，
# 由服务端按自己的默认决定这轮要不要想。
RESPONSES_THINKING_TIERS: dict[str, dict[str, object]] = {
    "off": {"reasoning": {"effort": "none"}},
    "adaptive": {},
    "high": {"reasoning": {"effort": "high"}},
    "max": {"reasoning": {"effort": "max"}},
}
# 协议 → 适配器与档位表。两条协议语义等价，选哪条看服务支持哪条。
_ADAPTERS = {"chat": (OpenAICompatibleChat, THINKING_TIERS),
             "responses": (ResponsesApiChat, RESPONSES_THINKING_TIERS)}


def _with_thinking(extra_body: dict[str, object], tier: str, protocol: str = "chat") -> dict[str, object]:
    """档位覆盖配置里的思考字段，其余私有参数原样保留。
    tier 由调用方保证合法（遍历 THINKING_TIERS 或写死 off），拿不到就该报错而不是静默降档。"""
    merged = {**extra_body, **_ADAPTERS[protocol][1][tier]}
    if protocol == "responses":
        # 档位只管 effort 这一个键：配置里 reasoning 下的其他键（如 summary）整块替换会丢掉。
        # adaptive 是「让服务端自己定」，连 effort 都不发。
        rest = {key: value for key, value in extra_body["reasoning"].items() if key != "effort"} \
            if isinstance(extra_body.get("reasoning"), dict) else {}
        effort = (_ADAPTERS[protocol][1][tier].get("reasoning") or {}).get("effort")
        reasoning = {**rest, **({"effort": effort} if effort else {})}
        merged.pop("reasoning", None)
        if reasoning:
            merged["reasoning"] = reasoning
    return merged


@dataclass(frozen=True)
class SharedRuntime:
    """跨用户共享的部分：适配器与向量模型只建一次。"""
    llm: AgentChatPort
    fallback: AgentChatPort
    classifier: AgentChatPort | None
    vision: VisionTranscriberPort | None
    chat_vision: VisionTranscriberPort | None
    web: WebSearchPort | None
    embedder: object | None
    # MCP 传输是无状态的，一个实例服务所有 server：每次调用自带地址与凭据。
    mcp_transport: StreamableHttpTransport | None = None
    reranker: object | None = None
    # (模型 key, 是否开思考) → 适配器。空表示没配远端，一律走 fallback。
    responders: dict[tuple[str, str], AgentChatPort] = field(default_factory=dict)

    def warm(self) -> None:
        """后台预热检索模型。两个都是首次用到才加载，实测嵌入 36s、重排 60s，
        而首轮检索两个都要碰——不预热就是让用户的第一个问题替我们等这一分钟。"""
        def load() -> None:
            for model, call in ((self.embedder, lambda m: m.embed_documents(["预热"])),
                                (self.reranker, lambda m: m.rerank(query="预热", documents=["预热"]))):
                if model is None:
                    continue
                try:
                    call(model)
                except Exception as error:  # 预热失败不该拖住启动，真正调用时还会再试一次
                    logger.warning("%s 预热失败：%s", type(model).__name__, error)
        threading.Thread(target=load, name="model-warmup", daemon=True).start()

    def close(self) -> None:
        for item in (self.llm, self.fallback, self.classifier, self.vision, self.chat_vision,
                     self.embedder, self.reranker, self.mcp_transport, *self.responders.values()):
            close = getattr(item, "close", None)
            if callable(close):
                try: close()
                except Exception: pass


CLOUD_PREFIX = "cloud:"


def _build_embedder(settings: Settings):
    name = settings.rag_embedding_model
    if not name:
        return None
    if name.startswith(CLOUD_PREFIX):
        if not settings.rag_cloud_configured:
            logger.warning("配了云端嵌入但缺 RAG_CLOUD_BASE_URL / RAG_CLOUD_API_KEY，语义检索关闭")
            return None
        return CloudEmbedder(
            api_key=settings.rag_cloud_api_key, base_url=settings.rag_cloud_base_url,
            model=name[len(CLOUD_PREFIX):], timeout_seconds=settings.llm_total_timeout_seconds,
        )
    return BgeEmbedder(
        model_name=name, device=settings.rag_embedding_device, batch_size=settings.rag_embedding_batch_size,
    )


def _build_reranker(settings: Settings):
    name = settings.rag_reranker_model
    if not name:
        return None
    if name.startswith(CLOUD_PREFIX):
        if not (settings.rag_cloud_configured and settings.rag_cloud_rerank_url):
            logger.warning("配了云端重排但缺 RAG_CLOUD_RERANK_URL / 凭据，重排关闭")
            return None
        return CloudReranker(
            api_key=settings.rag_cloud_api_key, url=settings.rag_cloud_rerank_url,
            model=name[len(CLOUD_PREFIX):], timeout_seconds=settings.llm_total_timeout_seconds,
        )
    return CrossEncoderReranker(model_name=name, device=settings.rag_embedding_device)


def build_shared_runtime(settings: Settings) -> SharedRuntime:
    fallback = DemoAgentChat()
    llm: AgentChatPort = fallback
    classifier: AgentChatPort | None = None
    responders: dict[tuple[str, str], AgentChatPort] = {}
    # 认的是「配了 OpenAI 兼容端点」而不是某个厂商名：写死厂商名会让别家的配置静默退回本地兜底。
    remote_ready = settings.enable_remote_llm and settings.remote_llm_configured
    if remote_ready:
        # 每个模型 × 每个思考档位一个实例。思考是每次请求的选项，但把它做成 chat() 的参数
        # 要动协议的全部实现；适配器本身很轻，多几个实例便宜得多。
        for choice in settings.models:
            if not choice.configured:
                continue
            adapter = _ADAPTERS[choice.protocol][0]
            if choice.protocol == "responses" and "thinking" in choice.extra_body:
                logger.warning("模型 %s 走 Responses 协议，extra_body 里的 thinking 不会生效；"
                               "思考深度请改配 reasoning.effort", choice.key)
            if choice.server_search and choice.protocol != "responses":
                logger.warning("模型 %s 开了 TEXT_SERVER_SEARCH，但它走 %s 协议——厂商端搜索只在 "
                               "Responses 协议上有，这一项已忽略", choice.key, choice.protocol)
            # 厂商端搜索只有 Responses 适配器认得这个参数，另一条协议连传都不能传。
            server_search = {"server_search": True} if choice.server_search and choice.protocol == "responses" else {}
            for tier in THINKING_TIERS:
                responders[(choice.key, tier)] = adapter(
                    api_key=choice.api_key, base_url=choice.base_url, model=choice.model,
                    provider=choice.provider, extra_body=_with_thinking(choice.extra_body, tier, choice.protocol),
                    connect_timeout_seconds=settings.llm_connect_timeout_seconds,
                    total_timeout_seconds=settings.llm_total_timeout_seconds,
                    max_output_tokens=settings.agent_max_output_tokens,
                    max_retries=settings.llm_max_retries,
                    **server_search,
                )
        first = settings.models[0]
        llm = responders.get((first.key, first.thinking_tier), fallback)
        # 学科分类器：超时更短、不重试，它跑在 turn 锁内、首个增量之前，所以固定用第一个模型
        # 并关掉思考——它的任务只是从清单里挑一个 id，也不需要联网。
        classifier = _ADAPTERS[first.protocol][0](
            api_key=first.api_key, base_url=first.base_url, model=first.model,
            provider=first.provider, extra_body=_with_thinking(first.extra_body, "off", first.protocol),
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=6, max_output_tokens=256, max_retries=0,
        )
    vision: VisionTranscriberPort | None = None
    chat_vision: VisionTranscriberPort | None = None
    if settings.enable_remote_llm and settings.vision_configured:
        def _vision(model: str, *, understand: bool) -> VisionTranscriberPort:
            return VisionOcrTranscriber(
                api_key=settings.vision_api_key, base_url=settings.vision_base_url, model=model,
                provider=settings.vision_provider, understand=understand,
                connect_timeout_seconds=settings.llm_connect_timeout_seconds,
                total_timeout_seconds=settings.llm_total_timeout_seconds,
                max_retries=settings.llm_max_retries,
            )

        vision = _vision(settings.vision_model, understand=False)
        # 拍照提问单独一个槽位；没配就复用上面那个，行为与以前一致。
        chat_vision = _vision(settings.vision_chat_model, understand=True) if settings.vision_chat_model else vision
    web: WebSearchPort | None = None
    if settings.enable_remote_llm and settings.web_search_configured:
        web = HttpWebAccess(
            api_key=settings.web_search_api_key,
            connect_timeout_seconds=settings.llm_connect_timeout_seconds,
            total_timeout_seconds=settings.web_timeout_seconds,
        )
    # 模型名写成 cloud:xxx 就走云端适配器，其余照旧本地加载。用一个前缀而不是另加一个
    # provider 开关：模型与它在哪跑是同一件事，分成两个配置项迟早会互相矛盾。
    embedder = _build_embedder(settings)
    reranker = _build_reranker(settings)
    # MCP 传输不看 enable_remote_llm：它连的是用户自己的 server，和对话模型是两回事。
    mcp_transport = StreamableHttpTransport(
        connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
        total_timeout_seconds=settings.mcp_timeout_seconds,
        allow_loopback=settings.mcp_allow_loopback,
    )
    return SharedRuntime(llm=llm, fallback=fallback, classifier=classifier, vision=vision, chat_vision=chat_vision, web=web, embedder=embedder, mcp_transport=mcp_transport, reranker=reranker, responders=responders)


def build_application(settings: Settings, shared: SharedRuntime | None = None) -> Application:
    """The one composition root: modules never instantiate repositories or adapters themselves."""
    runtime = shared or build_shared_runtime(settings)
    llm, fallback, classifier = runtime.llm, runtime.fallback, runtime.classifier
    vision, web, embedder, reranker = runtime.vision, runtime.web, runtime.embedder, runtime.reranker
    chat_vision = runtime.chat_vision or vision
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(settings.database_path)
    store.migrate()
    # 工具表与 profile 的一致性自检本来只在测试里跑，等于线上不设防：
    # 新加工具忘了归类能力，要到模型调用时才以「能力未开放」的形式冒出来。
    if problems := validate_profiles():
        raise RuntimeError("工具 profile 配置有问题：" + "；".join(problems))
    knowledge_repository, session_repository = KnowledgeRepository(store), SessionRepository(store)
    # 三个落盘 store 要先于 courses 建好：删课程要连带清掉它们的目录。
    notes, memory, wiki_store = NoteStore(settings.data_dir), MemoryStore(settings.data_dir), WikiStore(settings.data_dir)
    courses = CourseService(
        CourseRepository(store),
        # 删课程要连带清掉会话与教材，清理动作由各自的仓库提供，courses 只编排顺序。
        purge_sessions=session_repository.delete_course_sessions,
        purge_materials=knowledge_repository.delete_course_materials,
        purge_material=knowledge_repository.delete_material,
        purge_course_files=(notes.delete_course, wiki_store.delete_course, memory.delete_course),
    )
    knowledge = KnowledgeService(
        repository=knowledge_repository,
        settings=settings,
        wiki_is_enabled=lambda course_id: bool((course := courses.get_course(course_id)) and course.wiki_enabled),
        embedder=embedder,
        reranker=reranker,
        # 扫描版 PDF 的逐页 OCR 复用对话里那个 vision 槽位，不额外配一份
        transcriber=vision,
        wiki_store=wiki_store,
        responder=llm,
    )
    resolver = CourseResolver(courses, classifier=classifier)
    sessions = SessionService(
        session_repository, courses, resolver,
        vision=chat_vision,
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
    mcp = McpService(
        McpRepository(store),
        runtime.mcp_transport or StreamableHttpTransport(allow_loopback=settings.mcp_allow_loopback),
        allow_loopback=settings.mcp_allow_loopback,
    )
    # 内建 skill 目录随代码走（架构 §6）；导入的 skill 存库，启用后并入同一注册表。
    skills = SkillRegistry.from_directory(Path(__file__).resolve().parents[2] / "skills" / "builtin", user_skills=UserSkillStore(store))
    choices = settings.models
    default_key, default_tier = choices[0].key, choices[0].thinking_tier

    def select_responder(key: str | None, tier: str | None) -> AgentChatPort:
        """认不出的模型 key 或档位一律落回默认，别让一个过期的前端选择把整轮打挂。"""
        if not runtime.responders:
            return llm
        wanted = key if any(model.key == key for model in choices) else default_key
        level = tier if tier in THINKING_TIERS else default_tier
        return runtime.responders.get((wanted, level), llm)

    turns = TurnService(
        sessions, knowledge, planning, learning, llm, fallback,
        select_responder=select_responder,
        plan_writer=planning, evidence=learning, artifacts=ArtifactStore(store), compactions=CompactionStore(store), skills=skills, memory=memory,
        web=web, notes=notes, mcp=mcp,
        trace=TraceWriter(settings.data_dir / "traces"),
        search_limit=settings.top_k_results,
        history_token_budget=settings.agent_history_token_budget,
        context_token_limit=settings.agent_context_token_limit,
        partitions=settings.context_partitions,
        compact_threshold_ratio=settings.agent_compact_threshold_ratio,
    )
    return Application(settings, store, courses, knowledge, jobs, sessions, llm, turns, learning, planning, skills, notes, memory, mcp, vision, web)
