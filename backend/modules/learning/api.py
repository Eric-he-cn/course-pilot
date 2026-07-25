from __future__ import annotations
from typing import Protocol
from .models import ArchiveSummary


class ArchiveReaderPort(Protocol):
    def get_archive(self, *, course_id: str, limit: int = 20) -> ArchiveSummary: ...
