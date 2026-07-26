from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

from core.common import utc_now
from core.store import SQLiteStore

# frontmatter 必填字段（架构 §7.2）；缺一个就不注册，避免半成品 skill 被路由到。
_REQUIRED = ("name", "description", "when_to_use", "allowed_tools")
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
SOURCE_MAX_BYTES = 64 * 1024

# 一个 skill 目录/压缩包的收件上限，挡住压缩炸弹与误传整个仓库
BUNDLE_MAX_FILES = 40
BUNDLE_MAX_BYTES = 8 * 1024 * 1024
# 附带资料随正文一起进上下文，所以只收文本。这里没有 shell，脚本收了也执行不了。
BUNDLE_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".csv")
_ENTRY_NAME = "skill.md"

# 导入的 skill 只能拿到读工具与练习相关的写工具（架构 §6.1 的 policy 项）：
# 长期记忆、学习计划与加载别的 skill 都不在可授予范围内。
IMPORTABLE_TOOLS = (
    "search_materials", "list_materials", "get_plan", "get_archive",
    "concept_search", "emit_evidence", "artifact_read", "artifact_append",
)


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    when_to_use: str
    allowed_tools: tuple[str, ...]
    body: str
    content_hash: str  # trace 按 hash 聚合 judge 评分（§7.6）
    # 触发例句：帮助页直接展示，和规程同文件，改规程的人顺手改例句
    examples: tuple[str, ...] = ()
    origin: str = "builtin"
    status: str = "enabled"
    denied_tools: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """进系统提示的只有这一行；正文等 use_skill 时才注入。"""
        return f"- {self.name}：{self.when_to_use}"


def _parse_list(raw: str) -> tuple[str, ...]:
    inner = raw.strip().strip("[]")
    return tuple(item.strip().strip("'\"") for item in inner.split(",") if item.strip())


def parse_skill(text: str, *, source: str = "上传内容") -> SkillDefinition:
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(f"{source} 缺少 frontmatter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    missing = [key for key in _REQUIRED if not fields.get(key)]
    if missing:
        raise ValueError(f"{source} frontmatter 缺少 {missing}")
    name = fields["name"]
    if not _NAME.match(name):
        raise ValueError(f"skill 名称「{name}」不合法：只允许小写字母、数字、下划线与连字符，2-32 位")
    body = match.group(2).strip()
    if not body:
        raise ValueError(f"{source} 的正文为空")
    return SkillDefinition(
        name=name, description=fields["description"], when_to_use=fields["when_to_use"],
        allowed_tools=_parse_list(fields["allowed_tools"]), body=body,
        content_hash=hashlib.sha1(text.encode()).hexdigest()[:12],
        examples=tuple(item.strip() for item in fields.get("examples", "").split("|") if item.strip()),
    )


def load_skill(path: Path) -> SkillDefinition:
    return parse_skill(path.read_text(encoding="utf-8"), source=str(path))


def read_zip(raw: bytes) -> list[tuple[str, bytes]]:
    """把压缩包解到内存，不落盘。声明的体积不可信，按上限逐个截断读，超了就拒。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("不是有效的 ZIP 文件") from None
    entries = [item for item in archive.infolist() if not item.is_dir()]
    if len(entries) > BUNDLE_MAX_FILES:
        raise ValueError(f"压缩包里有 {len(entries)} 个文件，超过 {BUNDLE_MAX_FILES} 个的上限")
    members, total = [], 0
    for item in entries:
        with archive.open(item) as handle:
            data = handle.read(BUNDLE_MAX_BYTES + 1)
        total += len(data)
        if total > BUNDLE_MAX_BYTES:
            raise ValueError(f"压缩包解压后超过 {BUNDLE_MAX_BYTES // 1024 // 1024} MiB")
        members.append((item.filename, data))
    return members


def _clean_path(name: str) -> str | None:
    """路径只用于展示，但仍挡掉绝对路径与 ..，不给它们进正文的机会。"""
    normalized = name.replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized).parts if part != "."]
    if not parts or normalized.startswith("/") or ".." in parts:
        return None
    return "/".join(parts)


def merge_bundle(members: list[tuple[str, bytes]]) -> tuple[str, tuple[str, ...]]:
    """把一份 skill 目录压成单份文本：SKILL.md 打头，其余文本文件按路径追加在后面。

    附带资料是随规程一起注入的，没有按需读取——正文里 `references/x.md` 这类指路
    因此依然成立。代价是全部内容都占上下文，用 SOURCE_MAX_BYTES 兜住。
    """
    files = {path: raw for path, raw in ((_clean_path(name), raw) for name, raw in members) if path}
    entry = min(
        (path for path in files if path.lower().endswith(_ENTRY_NAME)),
        key=lambda path: (path.count("/"), path), default=None,
    )
    if entry is None:
        raise ValueError("这份 skill 里找不到 SKILL.md")
    try:
        text = files[entry].decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("SKILL.md 不是 UTF-8 文本") from None

    root = entry.rsplit("/", 1)[0] + "/" if "/" in entry else ""
    sections, skipped = [], []
    for path in sorted(path for path in files if path != entry):
        label = path[len(root):] if path.startswith(root) else path
        if not label.lower().endswith(BUNDLE_SUFFIXES):
            skipped.append(label)
            continue
        try:
            content = files[path].decode("utf-8").strip()
        except UnicodeDecodeError:
            skipped.append(label)
            continue
        if content:
            sections.append(f"\n\n## 附带文件：{label}\n\n{content}")
    # 只有一个文件时原样交出去，让 frontmatter 的校验口径跟单文件导入完全一致
    return (text.rstrip() + "".join(sections) if sections else text), tuple(skipped)


class UserSkillStore:
    """导入的 skill 存库；正文与 frontmatter 一起留档，便于用户在管理页复核。"""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def upsert(self, definition: SkillDefinition) -> None:
        now = utc_now()
        with self._store.write() as connection:
            connection.execute(
                "INSERT INTO user_skills(name, content_hash, source_text, description, when_to_use,"
                " allowed_tools_json, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET content_hash=excluded.content_hash,"
                " source_text=excluded.source_text, description=excluded.description,"
                " when_to_use=excluded.when_to_use, allowed_tools_json=excluded.allowed_tools_json,"
                " status=excluded.status, updated_at=excluded.updated_at",
                (definition.name, definition.content_hash, definition.body, definition.description,
                 definition.when_to_use, json.dumps(list(definition.allowed_tools), ensure_ascii=False),
                 definition.status, now, now),
            )

    def set_status(self, *, name: str, status: str) -> None:
        with self._store.write() as connection:
            connection.execute("UPDATE user_skills SET status = ?, updated_at = ? WHERE name = ?", (status, utc_now(), name))

    def delete(self, *, name: str) -> bool:
        with self._store.write() as connection:
            return connection.execute("DELETE FROM user_skills WHERE name = ?", (name,)).rowcount > 0

    def list_all(self) -> list[SkillDefinition]:
        with self._store.read() as connection:
            rows = connection.execute("SELECT * FROM user_skills ORDER BY name").fetchall()
        return [_from_row(row) for row in rows]

    def get(self, name: str) -> SkillDefinition | None:
        with self._store.read() as connection:
            row = connection.execute("SELECT * FROM user_skills WHERE name = ?", (name,)).fetchone()
        return _from_row(row) if row is not None else None


def _from_row(row) -> SkillDefinition:
    declared = tuple(json.loads(row["allowed_tools_json"]))
    granted = tuple(tool for tool in declared if tool in IMPORTABLE_TOOLS)
    return SkillDefinition(
        name=row["name"], description=row["description"], when_to_use=row["when_to_use"],
        allowed_tools=granted, body=row["source_text"], content_hash=row["content_hash"],
        origin="user", status=row["status"], denied_tools=tuple(tool for tool in declared if tool not in IMPORTABLE_TOOLS),
    )


class SkillRegistry:
    """内建 skill 目录 + 已启用的导入 skill；同名时内建优先。"""

    def __init__(self, definitions: dict[str, SkillDefinition], *, user_skills: UserSkillStore | None = None) -> None:
        self._definitions = definitions
        self._user_skills = user_skills

    @classmethod
    def from_directory(cls, directory: Path, *, user_skills: UserSkillStore | None = None) -> "SkillRegistry":
        definitions: dict[str, SkillDefinition] = {}
        for path in sorted(directory.glob("*/SKILL.md")) if directory.is_dir() else []:
            try:
                skill = load_skill(path)
            except ValueError as error:
                print(f"[skill] 跳过 {path}: {error}")
                continue
            definitions[skill.name] = skill
        return cls(definitions, user_skills=user_skills)

    def builtin_names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def _active(self) -> dict[str, SkillDefinition]:
        enabled = {}
        if self._user_skills is not None:
            enabled = {item.name: item for item in self._user_skills.list_all() if item.status == "enabled" and item.allowed_tools}
        return {**enabled, **self._definitions}

    def get(self, name: str) -> SkillDefinition | None:
        return self._active().get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._active())

    def summaries(self) -> str:
        return "\n".join(skill.summary for skill in self._active().values())

    def import_skill(self, text: str) -> SkillDefinition:
        """解析、按白名单收窄权限、默认关闭。权限不足的导入为草稿并记录被拒工具。"""
        if len(text.encode("utf-8")) > SOURCE_MAX_BYTES:
            raise ValueError(f"SKILL.md 超过 {SOURCE_MAX_BYTES // 1024} KiB")
        if self._user_skills is None:
            raise ValueError("当前实例未启用 skill 导入")
        parsed = parse_skill(text)
        if parsed.name in self._definitions:
            raise ValueError(f"「{parsed.name}」与内建 skill 同名，请改名后再导入")
        denied = tuple(tool for tool in parsed.allowed_tools if tool not in IMPORTABLE_TOOLS)
        granted = tuple(tool for tool in parsed.allowed_tools if tool in IMPORTABLE_TOOLS)
        if not granted:
            raise ValueError("声明的 allowed_tools 没有一个是可授予的，导入后无法工作；可用工具：" + "、".join(IMPORTABLE_TOOLS))
        # 权限不足不静默降权：导入为 permission_denied，由用户决定是改声明还是放弃。
        status = "permission_denied" if denied else "draft"
        definition = replace(parsed, origin="user", status=status, allowed_tools=granted, denied_tools=denied)
        self._user_skills.upsert(replace(definition, allowed_tools=parsed.allowed_tools))
        return definition

    def set_enabled(self, *, name: str, enabled: bool) -> SkillDefinition:
        if self._user_skills is None:
            raise ValueError("当前实例未启用 skill 导入")
        existing = self._user_skills.get(name)
        if existing is None:
            raise LookupError(name)
        if enabled and existing.denied_tools:
            raise ValueError("该 skill 申请了不可授予的工具（" + "、".join(existing.denied_tools) + "），修正声明后重新导入才能启用")
        self._user_skills.set_status(name=name, status="enabled" if enabled else "draft")
        return replace(existing, status="enabled" if enabled else "draft")

    def remove(self, *, name: str) -> None:
        if self._user_skills is None or not self._user_skills.delete(name=name):
            raise LookupError(name)

    def catalog(self) -> list[dict[str, object]]:
        builtin = [
            {"name": skill.name, "description": skill.description, "when_to_use": skill.when_to_use,
             "allowed_tools": list(skill.allowed_tools), "denied_tools": [], "origin": "builtin",
             "status": "enabled", "content_hash": skill.content_hash, "examples": list(skill.examples)}
            for skill in self._definitions.values()
        ]
        imported = [
            {"name": skill.name, "description": skill.description, "when_to_use": skill.when_to_use,
             "allowed_tools": list(skill.allowed_tools), "denied_tools": list(skill.denied_tools),
             "origin": "user", "status": skill.status, "content_hash": skill.content_hash,
             "examples": list(skill.examples)}
            for skill in (self._user_skills.list_all() if self._user_skills is not None else [])
        ]
        return builtin + imported
