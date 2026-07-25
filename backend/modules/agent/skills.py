from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# frontmatter 必填字段（架构 §7.2）；缺一个就不注册，避免半成品 skill 被路由到。
_REQUIRED = ("name", "description", "when_to_use", "allowed_tools")
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    when_to_use: str
    allowed_tools: tuple[str, ...]
    body: str
    content_hash: str  # trace 按 hash 聚合 judge 评分（§7.6）

    @property
    def summary(self) -> str:
        """进系统提示的只有这一行；正文等 use_skill 时才注入。"""
        return f"- {self.name}：{self.when_to_use}"


def _parse_list(raw: str) -> tuple[str, ...]:
    inner = raw.strip().strip("[]")
    return tuple(item.strip().strip("'\"") for item in inner.split(",") if item.strip())


def load_skill(path: Path) -> SkillDefinition:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{path} 缺少 frontmatter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    missing = [key for key in _REQUIRED if not fields.get(key)]
    if missing:
        raise ValueError(f"{path} frontmatter 缺少 {missing}")
    body = match.group(2).strip()
    return SkillDefinition(
        name=fields["name"], description=fields["description"], when_to_use=fields["when_to_use"],
        allowed_tools=_parse_list(fields["allowed_tools"]), body=body,
        content_hash=hashlib.sha1(text.encode()).hexdigest()[:12],
    )


class SkillRegistry:
    """内建 skill 目录的只读注册表；用户导入的 skill 后续按同一形状接入。"""

    def __init__(self, definitions: dict[str, SkillDefinition]) -> None:
        self._definitions = definitions

    @classmethod
    def from_directory(cls, directory: Path) -> "SkillRegistry":
        definitions: dict[str, SkillDefinition] = {}
        for path in sorted(directory.glob("*/SKILL.md")) if directory.is_dir() else []:
            try:
                skill = load_skill(path)
            except ValueError as error:
                print(f"[skill] 跳过 {path}: {error}")
                continue
            definitions[skill.name] = skill
        return cls(definitions)

    def get(self, name: str) -> SkillDefinition | None:
        return self._definitions.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def summaries(self) -> str:
        return "\n".join(skill.summary for skill in self._definitions.values())
