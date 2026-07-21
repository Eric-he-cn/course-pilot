from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class Course:
    id: str; name: str; color: str; wiki_enabled: bool; created_at: str; updated_at: str
