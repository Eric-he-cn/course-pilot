from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from app.common import new_id
from app.settings import Settings
from contracts.knowledge import KnowledgeHit, ResolvedKnowledgeScope

from .api import KnowledgeFeatureDisabledError, MaterialNotIndexedError
from .models import Job, Material
from .repository import KnowledgeRepository


class KnowledgeService:
    """Local-only RAG fallback and optional Wiki job skeleton.

    The service implements ``KnowledgeSearchPort`` by exposing ``search``.  Agent
    code receives the port through bootstrap and never receives this repository.
    """

    _ALLOWED_SUFFIXES = {".pdf", ".txt", ".md"}

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        settings: Settings,
        wiki_is_enabled: Callable[[str], bool] | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._wiki_is_enabled = wiki_is_enabled or (lambda _course_id: False)

    def upload_material(self, *, course_id: str, filename: str, mime_type: str, content: bytes) -> Material:
        safe_name = Path(filename).name
        suffix = Path(safe_name).suffix.lower()
        if not safe_name or suffix not in self._ALLOWED_SUFFIXES:
            raise ValueError("仅支持 PDF、TXT 或 MD 教材")
        if not content:
            raise ValueError("教材不能为空")
        if len(content) > self._settings.material_max_bytes:
            limit_mib = self._settings.material_max_bytes / (1024 * 1024)
            raise ValueError(f"教材超过 {limit_mib:g} MiB 上限")
        if suffix == ".pdf" and not content.startswith(b"%PDF"):
            raise ValueError("PDF 文件头无效")
        if suffix in {".txt", ".md"} and not mime_type.startswith("text/"):
            mime_type = "text/markdown" if suffix == ".md" else "text/plain"
        self._settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        # The display name never participates in a filesystem path.  A generated
        # name prevents same-named uploads from overwriting one another.
        storage_path = self._settings.uploads_dir / f"{new_id('upload')}{suffix}"
        storage_path.write_bytes(content)
        return self._repository.create_material(
            course_id=course_id, filename=safe_name, storage_path=storage_path, mime_type=mime_type, byte_size=len(content),
        )

    def list_materials(self, *, course_id: str) -> list[Material]:
        return self._repository.list_materials(course_id=course_id)

    def enqueue_index(self, *, material_id: str) -> Job:
        material = self._material_or_error(material_id)
        self._repository.set_material_status(material.id, "queued")
        return self._repository.create_job(type="index", material_id=material.id, course_id=material.course_id, retrieval_backend="sqlite_fts")

    def get_job(self, *, job_id: str) -> Job | None:
        return self._repository.get_job(job_id)

    def enqueue_wiki_build(self, *, material_id: str) -> Job:
        material = self._material_or_error(material_id)
        if not self._wiki_is_enabled(material.course_id):
            raise KnowledgeFeatureDisabledError("该课程尚未启用 Wiki")
        if material.index_status != "indexed":
            raise MaterialNotIndexedError("教材尚未完成索引")
        return self._repository.create_job(type="wiki", material_id=material.id, course_id=material.course_id)

    def run_job(self, *, job_id: str) -> Job | None:
        """Execute a previously persisted job on the local worker, never in HTTP."""
        job = self._repository.claim_queued_job(job_id)
        if job is None:
            return self.get_job(job_id=job_id)
        material = self._material_or_error(job.material_id)
        if job.type == "index":
            return self._run_index(job, material)
        if job.type == "wiki":
            return self._run_wiki(job, material)
        return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message="未知任务类型")

    def recover_jobs_after_restart(self) -> list[str]:
        return self._repository.recover_jobs_after_restart()

    def reject_queued_job(self, *, job_id: str, reason: str) -> Job | None:
        job = self.get_job(job_id=job_id)
        if job is None or job.status != "queued":
            return job
        return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=reason)

    def _run_wiki(self, job: Job, material: Material) -> Job:
        try:
            self._repository.update_job(job.id, status="running", stage="reading_index", progress=20)
            output = self._settings.data_dir / "wiki" / material.course_id
            output.mkdir(parents=True, exist_ok=True)
            source = self.search_course(course_id=material.course_id, query=Path(material.filename).stem, limit=6)
            outline = "\n\n".join(hit.content[:500] for hit in source) or "（教材已索引；等待后续 Wiki 解析。）"
            (output / f"{material.id}.md").write_text(f"# {material.filename}\n\n{outline}\n", encoding="utf-8")
            return self._repository.update_job(job.id, status="completed", stage="wiki_completed", progress=100)
        except Exception as error:
            return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=str(error))

    def search(self, *, scope: ResolvedKnowledgeScope, query: str, limit: int = 6) -> list[KnowledgeHit]:
        """Agent-only search: the course is a server-issued resolver result."""
        return self.search_course(course_id=scope.course_id, query=query, limit=limit)

    def search_course(self, *, course_id: str, query: str, limit: int = 6) -> list[KnowledgeHit]:
        """Explicit, course-scoped HTTP search use case."""
        if not query.strip():
            return []
        return self._repository.search(course_id=course_id, query=query, limit=max(1, min(limit, 20)))

    def health(self) -> dict[str, object]:
        try:
            migration_version = self._repository.health_check()
            database: dict[str, object] = {"ok": True, "migration_version": migration_version}
        except Exception as error:
            database = {"ok": False, "error": str(error)}
        return {
            "database": database,
            "rag": {"ok": bool(database["ok"]), "backend": "sqlite_fts_fallback"},
        }

    def _run_index(self, job: Job, material: Material) -> Job:
        try:
            self._repository.set_material_status(material.id, "indexing")
            self._repository.update_job(job.id, status="running", stage="extracting", progress=15)
            path = self._repository.material_storage_path(material.id)
            if path is None or not path.is_file():
                raise ValueError("教材文件不存在")
            text = self._extract_text(path, material.filename)
            self._repository.update_job(job.id, status="running", stage="chunking", progress=45)
            chunks = self._chunk(text)
            if not chunks:
                raise ValueError("未能从教材中提取可检索文本")
            self._repository.update_job(job.id, status="running", stage="indexing", progress=75)
            self._repository.replace_chunks(material_id=material.id, course_id=material.course_id, chunks=chunks)
            self._repository.set_material_status(material.id, "indexed")
            return self._repository.update_job(job.id, status="completed", stage="completed", progress=100, retrieval_backend="sqlite_fts")
        except Exception as error:
            self._repository.set_material_status(material.id, "failed")
            return self._repository.update_job(job.id, status="failed", stage="failed", progress=100, error_message=str(error), retrieval_backend="sqlite_fts")

    def _extract_text(self, path: Path, filename: str) -> str:
        raw = path.read_bytes()
        if Path(filename).suffix.lower() in {".txt", ".md"}:
            return raw.decode("utf-8", errors="replace").strip()
        try:
            # Avoid asking pypdf to parse clearly incomplete data: besides being
            # noisy, it cannot improve on the fallback below.
            if b"%%EOF" not in raw:
                raise ValueError("incomplete PDF")
            from pypdf import PdfReader  # Optional at unit-test time; declared runtime dependency.
            return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages).strip()
        except Exception:
            # Some valid PDFs are image-only or use unsupported encodings.  Fall
            # through to the small local fallback before reporting extraction
            # failure to the job.
            pass
        # Dependency-free PDF fallback: support common literal-string text operators.
        fragments = re.findall(rb"\(([^()]*)\)\s*(?:Tj|TJ)", raw)
        return "\n".join(fragment.decode("latin-1", errors="replace") for fragment in fragments).strip()

    def _chunk(self, text: str) -> list[tuple[int | None, str]]:
        normalized = re.sub(r"\r\n?", "\n", text).strip()
        size, overlap = self._settings.chunk_size, min(self._settings.chunk_overlap, self._settings.chunk_size - 1)
        chunks: list[tuple[int | None, str]] = []
        start = 0
        while start < len(normalized):
            end = min(len(normalized), start + size)
            if end < len(normalized):
                split = normalized.rfind("\n", start + max(1, size // 2), end)
                if split > start:
                    end = split
            value = normalized[start:end].strip()
            if value:
                chunks.append((None, value))
            if end >= len(normalized):
                break
            start = max(end - overlap, start + 1)
        return chunks

    def _material_or_error(self, material_id: str) -> Material:
        material = self._repository.get_material(material_id)
        if material is None:
            raise ValueError("教材不存在")
        return material
