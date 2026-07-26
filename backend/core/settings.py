from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path


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

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    @property
    def thinking_tier(self) -> str:
        """从配置里的 extra_body 推出默认档位；档位名与 bootstrap.THINKING_TIERS 对应。"""
        thinking = self.extra_body.get("thinking")
        if not isinstance(thinking, dict):
            # 没配就别替用户拍一个深度：adaptive 是「让模型自己决定这轮要不要想」。
            return "adaptive"
        kind, effort = thinking.get("type"), thinking.get("effort")
        if kind == "disabled":
            return "off"
        if kind == "adaptive":
            return "adaptive"
        return "max" if effort == "max" else "high"


def _read_models(value) -> tuple[ModelChoice, ...]:
    """第一个模型用不带后缀的 TEXT_*；之后按 TEXT_MODEL_2、_3… 递增，扫到断号为止。
    同一家的第二个模型只需要填 TEXT_MODEL_n 一行，其余字段继承第一个。"""
    first = ModelChoice(
        key="1", label="模型一",
        provider=value("TEXT_PROVIDER", "openai_compatible"), base_url=value("TEXT_BASE_URL"),
        api_key=value("TEXT_API_KEY"), model=value("TEXT_MODEL"),
        extra_body=_parse_extra_body(value("TEXT_EXTRA_BODY")),
    )
    models = [first]
    for index in range(2, 10):
        model = value(f"TEXT_MODEL_{index}").strip()
        if not model:
            break
        raw_extra = value(f"TEXT_EXTRA_BODY_{index}").strip()
        models.append(ModelChoice(
            key=str(index), label=f"模型{_CN_DIGITS[index - 1]}",
            provider=value(f"TEXT_PROVIDER_{index}") or first.provider,
            base_url=value(f"TEXT_BASE_URL_{index}") or first.base_url,
            api_key=value(f"TEXT_API_KEY_{index}") or first.api_key,
            model=model,
            extra_body=_parse_extra_body(raw_extra, f"TEXT_EXTRA_BODY_{index}") if raw_extra else dict(first.extra_body),
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
    # vision 槽位（OCR）：未配置时附件上传返回 feature_disabled。
    vision_provider: str = ""
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""
    attachment_max_bytes: int = 10 * 1024 * 1024
    attachment_max_pixels: int = 12_000_000
    # Agent 历史预算，以字符数保守近似 token（按 1 字符 ≤ 1 token，宁少勿超窗）。
    # 默认对应 512K 软窗口里 128K 的历史份额。
    agent_history_token_budget: int = 128_000
    # 整轮上下文的软窗口，同样以字符数近似 token（架构 §5.5 的 512K 软窗口）。
    agent_context_char_limit: int = 512_000
    # 历史占到预算这个比例就压缩；调小可以在短对话上验证压缩链路。
    agent_compact_threshold_ratio: float = 0.7
    # 缺请求头时落到哪个用户：本地无认证应用里这是合理默认，也让脚本不用带头就能跑。
    default_user: str = "local"
    # 联网检索（SerpAPI）：未配置或未开启远端调用时，network 类工具整体不下发。
    web_search_api_key: str = ""
    web_timeout_seconds: float = 20
    # 厂商私有的请求字段（如关闭思考模式），原样并入 chat/completions 请求体。
    text_extra_body: dict[str, object] = field(default_factory=dict)
    # 可选的对话模型。第一项等同上面那组 text_* 字段，界面按它们的顺序给用户切换。
    text_models: tuple[ModelChoice, ...] = ()

    def for_workspace(self, workspace_dir: Path) -> "Settings":
        """某个用户工作区的 Settings。三个路径字段必须一起换——只改 data_dir
        会把数据库和教材留在共享目录，那正是最典型的"漏一个隔离通道"。"""
        return replace(
            self, data_dir=workspace_dir,
            database_path=workspace_dir / "coursepilot.db",
            uploads_dir=workspace_dir / "materials",
        )

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
        ),)

    @property
    def web_search_configured(self) -> bool:
        return bool(self.web_search_api_key)

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
        return cls(
            data_dir, data_dir / "coursepilot.db", data_dir / "materials",
            value("TEXT_PROVIDER", "openai_compatible"), value("TEXT_BASE_URL"),
            value("TEXT_API_KEY"), value("TEXT_MODEL"),
            value("COURSEPILOT_ENABLE_REMOTE_LLM", "0").lower() in {"1", "true", "yes"},
            max(100, int(value("RAG_CHUNK_SIZE", "600"))), max(0, int(value("RAG_CHUNK_OVERLAP", "120"))),
            max(1, int(value("RAG_TOP_K_RESULTS", "6"))),
            max(1, int(value("MATERIAL_MAX_BYTES", str(100 * 1024 * 1024)))),
            max(1, int(value("BACKGROUND_JOB_WORKERS", "1"))),
            max(1, int(value("BACKGROUND_JOB_QUEUE_CAPACITY", "8"))),
            max(0.1, float(value("LLM_CONNECT_TIMEOUT_SECONDS", "10"))),
            max(1.0, float(value("LLM_TOTAL_TIMEOUT_SECONDS", "180"))),
            max(0, int(value("LLM_MAX_RETRIES", "2"))),
            max(256, int(value("AGENT_MAX_OUTPUT_TOKENS", "8192"))),
            value("RAG_EMBEDDING_MODEL", "BAAI/bge-base-zh-v1.5"),
            value("RAG_EMBEDDING_DEVICE", "auto"),
            max(1, int(value("RAG_EMBEDDING_BATCH_SIZE", "256"))),
            value("VISION_PROVIDER"),
            value("VISION_BASE_URL"),
            value("VISION_API_KEY"),
            value("VISION_MODEL"),
            max(1, int(value("ATTACHMENT_MAX_BYTES", str(10 * 1024 * 1024)))),
            max(1, int(value("ATTACHMENT_MAX_PIXELS", "12000000"))),
            max(1024, int(value("AGENT_HISTORY_TOKEN_BUDGET", "128000"))),
            max(2048, int(value("AGENT_CONTEXT_CHAR_LIMIT", "512000"))),
            min(0.95, max(0.05, float(value("AGENT_COMPACT_THRESHOLD_RATIO", "0.7")))),
            value("COURSEPILOT_DEFAULT_USER", "local"),
            value("RESEARCH_SERPAPI_API_KEY"),
            max(1.0, float(value("WEB_TIMEOUT_SECONDS", "20"))),
            _parse_extra_body(value("TEXT_EXTRA_BODY")),
            _read_models(value),
        )
