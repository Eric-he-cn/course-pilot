from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from core import hardware


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _parse_extra_body(raw: str, name: str = "TEXT_EXTRA_BODY") -> dict[str, object]:
    """配置错了就在启动时说清楚，别留到第一次对话才炸。"""
    text = raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} 不是合法 JSON：{error}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} 必须是 JSON 对象")
    return parsed


_CN_DIGITS = "一二三四五六七八九"

# 模型接入协议：chat 打 /chat/completions，responses 打 /responses。语义等价，选哪条看服务支持哪条。
PROTOCOLS = ("chat", "responses")


def _parse_protocol(raw: str, name: str = "TEXT_PROTOCOL") -> str:
    """配错了在启动时说清楚，别留到第一次对话才发现请求打在了不存在的端点上。"""
    protocol = raw.strip().lower() or "chat"
    if protocol not in PROTOCOLS:
        raise ValueError(f"{name} 只能是 {' 或 '.join(PROTOCOLS)}，收到：{raw.strip()}")
    return protocol


def _flag(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes"}

# 软窗口各分区占的比例，对应架构 §5.5 那张表。合计 92.2%，余下留给模型输出与估算误差。
# 换一个窗口更小的模型只改 AGENT_MODEL_CONTEXT_WINDOW，各分区按同样比例一起缩。
CONTEXT_PARTITION_RATIOS: dict[str, float] = {
    "system": 0.125,       # 静态规则 + 教材清单 + 能力摘要 + 练习状态
    "question": 0.09375,   # 本轮用户消息（含图片转录）与它派生的检索参数
    "history": 0.25,       # 最近会话历史
    "knowledge": 0.15625,  # 长期记忆 + 对话摘要 + 知识页目录
    "evidence": 0.234375,  # 种子检索取回的教材与知识页正文
    "skill": 0.0625,       # 当前 skill 正文
}


@dataclass(frozen=True)
class PartitionLimits:
    """软窗口切给各分区的 token 配额。某段超了先裁它自己，不借用别的分区。"""

    system: int
    question: int
    history: int
    knowledge: int
    evidence: int
    skill: int

    @classmethod
    def from_window(cls, soft_window: int) -> "PartitionLimits":
        return cls(**{name: max(256, int(soft_window * ratio))
                      for name, ratio in CONTEXT_PARTITION_RATIOS.items()})


@dataclass(frozen=True)
class ModelChoice:
    """一个可选的对话模型。序号即 key，标签不带性能判断——用户要的是「模型一/二」。"""

    key: str
    label: str
    provider: str
    base_url: str
    api_key: str
    model: str
    extra_body: dict[str, object] = field(default_factory=dict)
    # 走哪条协议，见 PROTOCOLS。
    protocol: str = "chat"
    # 是否让厂商在它那边联网搜索（只有 responses 协议有这个能力），默认关。
    server_search: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def thinking_tier(self) -> str:
        """从配置里的 extra_body 推出默认档位；档位名与 bootstrap 的档位表对应。
        没配就别替用户拍一个深度：adaptive 是「让模型自己决定这轮要不要想」。"""
        if self.protocol == "responses":
            # Responses 协议下思考深度走 reasoning.effort。界面只有四档，厂商的 low/medium/xhigh
            # 都落到 high——与 chat 那套一致（那边非 max 的 effort 同样归 high）。
            effort = (self.extra_body.get("reasoning") or {}).get("effort") \
                if isinstance(self.extra_body.get("reasoning"), dict) else None
            return {"none": "off", "minimal": "off", "max": "max"}.get(str(effort), "high") if effort else "adaptive"
        thinking = self.extra_body.get("thinking")
        if not isinstance(thinking, dict):
            return "adaptive"
        kind, effort = thinking.get("type"), thinking.get("effort")
        if kind == "disabled":
            return "off"
        if kind == "adaptive":
            return "adaptive"
        return "max" if effort == "max" else "high"


# 已在真实语料上标定过阈值的重排模型。标定方法与完整数据见 adapters/reranker.py 的注释。
# rerank 分是无界 logit 过 sigmoid 得来的，尺度跟着模型走，换模型必须重新标定。
CALIBRATED_RERANK_THRESHOLDS: dict[str, float] = {
    "BAAI/bge-reranker-v2-m3": 0.3,
    "BAAI/bge-reranker-base": 0.3,
    # 云端模型的尺度和本地完全不同：同一批用例下 gte-rerank-v2 的正例中位只有 0.45、
    # 负例最高 0.143，套 0.3 会把大半正例也滤掉。间距 0.027 比本地的 0.118 窄得多，
    # 也就是分离能力更弱——云端是给跑不动本地模型的机器用的，不是更好的选择。
    "cloud:gte-rerank-v2": 0.17,
}


def _rerank_threshold(raw: str, model_name: str) -> float:
    """没显式配阈值时，只对标定过的模型启用过滤。

    给一个没标定过的模型套上别人的阈值，等于随机误杀教材内容——那种情况宁可不过滤。
    """
    if raw.strip():
        return min(1.0, max(0.0, float(raw)))
    return CALIBRATED_RERANK_THRESHOLDS.get(model_name, 0.0)


def _read_models(value) -> tuple[ModelChoice, ...]:
    """第一个模型用不带后缀的 TEXT_*；之后按 TEXT_MODEL_2、_3… 递增，扫到断号为止。
    同一家的第二个模型只需要填 TEXT_MODEL_n 一行，其余字段继承第一个。"""
    first = ModelChoice(
        key="1", label="模型一",
        provider=value("TEXT_PROVIDER", "openai_compatible"), base_url=value("TEXT_BASE_URL"),
        api_key=value("TEXT_API_KEY"), model=value("TEXT_MODEL"),
        extra_body=_parse_extra_body(value("TEXT_EXTRA_BODY")),
        protocol=_parse_protocol(value("TEXT_PROTOCOL")),
        server_search=_flag(value("TEXT_SERVER_SEARCH")),
    )
    models = [first]
    for index in range(2, 10):
        model = value(f"TEXT_MODEL_{index}").strip()
        if not model:
            break
        raw_extra = value(f"TEXT_EXTRA_BODY_{index}").strip()
        raw_protocol = value(f"TEXT_PROTOCOL_{index}").strip()
        raw_search = value(f"TEXT_SERVER_SEARCH_{index}").strip()
        models.append(ModelChoice(
            key=str(index), label=f"模型{_CN_DIGITS[index - 1]}",
            provider=value(f"TEXT_PROVIDER_{index}") or first.provider,
            base_url=value(f"TEXT_BASE_URL_{index}") or first.base_url,
            api_key=value(f"TEXT_API_KEY_{index}") or first.api_key,
            model=model,
            extra_body=_parse_extra_body(raw_extra, f"TEXT_EXTRA_BODY_{index}") if raw_extra else dict(first.extra_body),
            protocol=_parse_protocol(raw_protocol, f"TEXT_PROTOCOL_{index}") if raw_protocol else first.protocol,
            server_search=_flag(raw_search) if raw_search else first.server_search,
        ))
    return tuple(models)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    uploads_dir: Path
    text_provider: str
    text_base_url: str
    text_api_key: str
    text_model: str
    enable_remote_llm: bool
    chunk_size: int
    chunk_overlap: int
    top_k_results: int
    # Defaults keep Settings a stable construction boundary for tests and
    # adapters that only override the fields they need.
    material_max_bytes: int = 100 * 1024 * 1024
    background_job_workers: int = 1
    background_job_queue_capacity: int = 8
    llm_connect_timeout_seconds: float = 10
    llm_total_timeout_seconds: float = 180
    llm_max_retries: int = 2
    agent_max_output_tokens: int = 8192
    # 空字符串表示关闭语义检索（测试默认关闭，避免加载模型）。
    rag_embedding_model: str = ""
    rag_embedding_device: str = "auto"
    rag_embedding_batch_size: int = 256
    # 召回阶段的余弦下限，默认 0（关闭）。余弦分的绝对值不跨库可比——实测正例最低 0.375、
    # 负例最高 0.525，任何取值都会误杀。判「有没有搜到」交给下面的 rerank 分。
    rag_min_similarity: float = 0.0
    # 重排：空字符串关闭，此时退回 RRF 排序并且阈值不生效。
    rag_reranker_model: str = ""
    # 送进 rerank 的候选数（词面 top-N ∪ 语义 top-N 去重后截断）
    rag_rerank_candidates: int = 20
    # rerank 分下限，逐条过滤，全部低于它就等于这次没搜到。标定见 adapters/reranker.py
    # 默认 0（不过滤），与 _rerank_threshold 对未标定模型的处置一致。
    rag_min_rerank_score: float = 0.0
    # vision 槽位（OCR）：未配置时附件上传返回 feature_disabled。
    vision_provider: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""
    # 扫描版 PDF 逐页转录用：专用 OCR 模型最便宜，量大
    vision_model: str = ""
    # 拍照提问用：留空则复用上面那个。两件事对模型的要求不同——转录只要抄字，
    # 拍题要看懂手写、图表与版面，专用 OCR 模型在这上面明显更差。
    vision_chat_model: str = ""
    attachment_max_bytes: int = 10 * 1024 * 1024
    attachment_max_pixels: int = 12_000_000
    # 所配模型的上下文窗口，按 context.estimate_tokens 折算的 token 计（中英分段估算，偏高估）。
    # 换模型时改这一个数，软窗口与各分区配额都跟着它推导。
    agent_model_context_window: int = 1_024_000
    # Agent 历史预算，默认是软窗口的 history 份额。
    agent_history_token_budget: int = 128_000
    # 整轮上下文的软窗口，默认取模型窗口的一半，留出输出与估算误差的余量。
    agent_context_token_limit: int = 512_000
    # 历史占到预算这个比例就压缩；调小可以在短对话上验证压缩链路。
    agent_compact_threshold_ratio: float = 0.7
    # 缺请求头时落到哪个用户：本地无认证应用里这是合理默认，也让脚本不用带头就能跑。
    default_user: str = "local"
    # 联网检索（SerpAPI）：未配置或未开启远端调用时，network 类工具整体不下发。
    web_search_api_key: str = ""
    web_timeout_seconds: float = 20
    # MCP 接入。默认拒绝指向本机与内网的地址；打开这个开关只放开回环（127.0.0.0/8、::1），
    # 供本机跑着一台 MCP server 的情形使用。私网与 169.254.169.254 这类元数据端点
    # 无论开关如何都拒绝。
    mcp_allow_loopback: bool = False
    mcp_connect_timeout_seconds: float = 10
    mcp_timeout_seconds: float = 30
    # 厂商私有的请求字段（如关闭思考模式），原样并入请求体。
    text_extra_body: dict[str, object] = field(default_factory=dict)
    # 模型接入协议，见 PROTOCOLS。默认 chat，与加这个字段之前的行为一致。
    text_protocol: str = "chat"
    # 厂商端联网搜索，默认关。搜索由厂商执行，结果直接进模型上下文，不经过本地的
    # 不可信内容前缀，也产不出可点开的引用；开它是一次明确的安全取舍。
    text_server_search: bool = False
    # Responses 协议上，厂商标成 commentary 的过场叙述改走思考流，不进正文。默认开。
    text_commentary_to_reasoning: bool = True
    # 可选的对话模型。第一项等同上面那组 text_* 字段，界面按它们的顺序给用户切换。
    text_models: tuple[ModelChoice, ...] = ()
    # 本机探测结果。配置写 auto 时按它选模型；写死模型名时它只用于健康上报。
    hardware: dict[str, object] = field(default_factory=dict)
    # 云端检索：模型名写成 cloud:xxx 时用这组凭据。给跑不动本地模型的机器留的路，
    # 代价是每次检索多一个网络往返，而且教材内容会发到服务商那里。
    rag_cloud_base_url: str = ""
    rag_cloud_api_key: str = ""
    rag_cloud_rerank_url: str = ""

    def for_workspace(self, workspace_dir: Path) -> "Settings":
        """某个用户工作区的 Settings。三个路径字段必须一起换——只改 data_dir
        会把数据库和教材留在共享目录，那正是最典型的"漏一个隔离通道"。"""
        return replace(
            self, data_dir=workspace_dir,
            database_path=workspace_dir / "coursepilot.db",
            uploads_dir=workspace_dir / "materials",
        )

    @property
    def context_partitions(self) -> PartitionLimits:
        """按软窗口切出的分区配额；历史那一份用配置里的实际值，别和已生效的预算说两套。"""
        return replace(PartitionLimits.from_window(self.agent_context_token_limit),
                       history=self.agent_history_token_budget)

    @property
    def remote_llm_configured(self) -> bool:
        return bool(self.text_api_key and self.text_base_url and self.text_model)

    @property
    def models(self) -> tuple[ModelChoice, ...]:
        """直接构造 Settings（测试、脚本）时不必填 text_models，这里从单模型字段兜出一个。"""
        if self.text_models:
            return self.text_models
        return (ModelChoice(
            key="1", label="模型一", provider=self.text_provider, base_url=self.text_base_url,
            api_key=self.text_api_key, model=self.text_model, extra_body=dict(self.text_extra_body),
            protocol=self.text_protocol, server_search=self.text_server_search,
        ),)

    @property
    def web_search_configured(self) -> bool:
        return bool(self.web_search_api_key)

    @property
    def rag_cloud_configured(self) -> bool:
        return bool(self.rag_cloud_api_key and self.rag_cloud_base_url)

    @property
    def vision_configured(self) -> bool:
        return bool(self.vision_api_key and self.vision_base_url and self.vision_model)

    @classmethod
    def from_environment(cls, project_root: Path | None = None) -> "Settings":
        root = project_root or Path(__file__).resolve().parents[2]
        dotenv = _read_dotenv(root / ".env")
        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, dotenv.get(name, default))
        data_dir = Path(value("STORAGE_DATA_DIR", str(root / "data")))
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        # 配置写 auto 时按本机内存与加速器分档，见 core/hardware.py。写死模型名就照配置来。
        machine = hardware.probe()
        embedding_model = hardware.resolve("embedding", value("RAG_EMBEDDING_MODEL", "BAAI/bge-base-zh-v1.5"), machine)
        reranker_model = hardware.resolve("reranker", value("RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"), machine)
        # 软窗口与历史预算默认都从模型窗口推导，显式配了才用配的值；两者都不许超过上一层。
        window = max(4096, int(value("AGENT_MODEL_CONTEXT_WINDOW", "1024000")))
        raw_limit = value("AGENT_CONTEXT_TOKEN_LIMIT").strip()
        context_limit = min(window, max(2048, int(raw_limit))) if raw_limit else window // 2
        raw_history = value("AGENT_HISTORY_TOKEN_BUDGET").strip()
        history_budget = (min(context_limit, max(1024, int(raw_history))) if raw_history
                          else PartitionLimits.from_window(context_limit).history)
        return cls(
            data_dir=data_dir,
            database_path=data_dir / "coursepilot.db",
            uploads_dir=data_dir / "materials",
            text_provider=value("TEXT_PROVIDER", "openai_compatible"),
            text_base_url=value("TEXT_BASE_URL"),
            text_api_key=value("TEXT_API_KEY"),
            text_model=value("TEXT_MODEL"),
            enable_remote_llm=value("COURSEPILOT_ENABLE_REMOTE_LLM", "0").lower() in {"1", "true", "yes"},
            chunk_size=max(100, int(value("RAG_CHUNK_SIZE", "600"))),
            chunk_overlap=max(0, int(value("RAG_CHUNK_OVERLAP", "120"))),
            top_k_results=max(1, int(value("RAG_TOP_K_RESULTS", "6"))),
            material_max_bytes=max(1, int(value("MATERIAL_MAX_BYTES", str(100 * 1024 * 1024)))),
            background_job_workers=max(1, int(value("BACKGROUND_JOB_WORKERS", "1"))),
            background_job_queue_capacity=max(1, int(value("BACKGROUND_JOB_QUEUE_CAPACITY", "8"))),
            llm_connect_timeout_seconds=max(0.1, float(value("LLM_CONNECT_TIMEOUT_SECONDS", "10"))),
            llm_total_timeout_seconds=max(1.0, float(value("LLM_TOTAL_TIMEOUT_SECONDS", "180"))),
            llm_max_retries=max(0, int(value("LLM_MAX_RETRIES", "2"))),
            agent_max_output_tokens=max(256, int(value("AGENT_MAX_OUTPUT_TOKENS", "8192"))),
            rag_embedding_model=embedding_model,
            rag_embedding_device=value("RAG_EMBEDDING_DEVICE", "auto"),
            rag_embedding_batch_size=max(1, int(value("RAG_EMBEDDING_BATCH_SIZE", "256"))),
            rag_min_similarity=min(1.0, max(0.0, float(value("RAG_MIN_SIMILARITY", "0")))),
            rag_reranker_model=reranker_model,
            rag_rerank_candidates=max(1, int(value("RAG_RERANK_CANDIDATES", "20"))),
            rag_min_rerank_score=_rerank_threshold(value("RAG_MIN_RERANK_SCORE"), reranker_model),
            vision_provider=value("VISION_PROVIDER"),
            vision_base_url=value("VISION_BASE_URL"),
            vision_api_key=value("VISION_API_KEY"),
            vision_model=value("VISION_MODEL"),
            vision_chat_model=value("VISION_CHAT_MODEL"),
            attachment_max_bytes=max(1, int(value("ATTACHMENT_MAX_BYTES", str(10 * 1024 * 1024)))),
            attachment_max_pixels=max(1, int(value("ATTACHMENT_MAX_PIXELS", "12000000"))),
            agent_model_context_window=window,
            agent_history_token_budget=history_budget,
            agent_context_token_limit=context_limit,
            agent_compact_threshold_ratio=min(0.95, max(0.05, float(value("AGENT_COMPACT_THRESHOLD_RATIO", "0.7")))),
            default_user=value("COURSEPILOT_DEFAULT_USER", "local"),
            web_search_api_key=value("RESEARCH_SERPAPI_API_KEY"),
            web_timeout_seconds=max(1.0, float(value("WEB_TIMEOUT_SECONDS", "20"))),
            mcp_allow_loopback=value("MCP_ALLOW_LOOPBACK", "0").lower() in {"1", "true", "yes"},
            mcp_connect_timeout_seconds=max(0.1, float(value("MCP_CONNECT_TIMEOUT_SECONDS", "10"))),
            mcp_timeout_seconds=max(1.0, float(value("MCP_TIMEOUT_SECONDS", "30"))),
            text_extra_body=_parse_extra_body(value("TEXT_EXTRA_BODY")),
            text_protocol=_parse_protocol(value("TEXT_PROTOCOL")),
            text_server_search=_flag(value("TEXT_SERVER_SEARCH")),
            text_commentary_to_reasoning=_flag(value("TEXT_COMMENTARY_TO_REASONING", "1")),
            text_models=_read_models(value),
            hardware=machine.as_dict(),
            rag_cloud_base_url=value("RAG_CLOUD_BASE_URL"),
            rag_cloud_api_key=value("RAG_CLOUD_API_KEY"),
            rag_cloud_rerank_url=value("RAG_CLOUD_RERANK_URL"),
        )
