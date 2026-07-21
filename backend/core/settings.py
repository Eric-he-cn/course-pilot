from __future__ import annotations

import os
from dataclasses import dataclass
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

    @property
    def remote_llm_configured(self) -> bool:
        return bool(self.text_api_key and self.text_base_url and self.text_model)

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
            value("TEXT_PROVIDER", "deepseek"), value("TEXT_BASE_URL", "https://api.deepseek.com"),
            value("TEXT_API_KEY"), value("TEXT_MODEL", "deepseek-v4-flash"),
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
        )
