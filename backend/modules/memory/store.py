from __future__ import annotations

import re
import threading
from pathlib import Path

# 受管区块：只有 marker 之间的内容会被 Agent 覆盖，用户手写的段落不动（架构 §8）。
_BLOCK = "<!-- agent:managed:{section} -->\n{content}\n<!-- /agent:managed:{section} -->"
_BLOCK_PATTERN = "<!-- agent:managed:{section} -->.*?<!-- /agent:managed:{section} -->"
_SECTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
_MAX_SECTION_CHARS = 2000
_MAX_FILE_CHARS = 20_000

_USER_HEADER = """# 用户画像

记录学习习惯、讲解偏好与长期目标；跨课程生效。掌握度数值、错题与复习排期不写这里。
"""
_COURSE_HEADER = """# 课程记忆

记录学到哪、遗留问题与和用户的约定。掌握度数值、错题与复习排期不写这里，它们在事件流里。
"""


class MemoryStore:
    """定性记忆的 markdown 文件存储：全局画像 + 课程情景记忆。

    与定量状态严格分工——掌握度、错题、排期只存事件流，这里只放叙述性内容。
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _user_path(self) -> Path:
        return self._data_dir / "user.md"

    def _course_path(self, course_id: str) -> Path:
        return self._data_dir / "courses" / course_id / "memory.md"

    def read_user(self) -> str:
        return self._read(self._user_path())

    def read_course(self, course_id: str) -> str:
        return self._read(self._course_path(course_id))

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        except OSError:
            return ""

    def patch(self, *, scope: str, section: str, content: str, course_id: str | None = None) -> str:
        """整块替换某个受管区块；区块不存在就追加。返回一句写入结果说明。"""
        if scope not in {"user", "course"}:
            raise ValueError("scope 只能是 user 或 course")
        if scope == "course" and not course_id:
            raise ValueError("课程记忆需要 course_id")
        if not _SECTION_NAME.match(section or ""):
            raise ValueError("section 只能是小写字母、数字和下划线，且以字母开头")
        clean = content.strip()
        if not clean:
            raise ValueError("content 不能为空")
        if len(clean) > _MAX_SECTION_CHARS:
            raise ValueError(f"单个区块不能超过 {_MAX_SECTION_CHARS} 字")

        path = self._user_path() if scope == "user" else self._course_path(course_id or "")
        header = _USER_HEADER if scope == "user" else _COURSE_HEADER
        block = _BLOCK.format(section=section, content=clean)
        with self._lock:
            existing = self._read(path)
            body = existing or header.strip()
            pattern = re.compile(_BLOCK_PATTERN.format(section=re.escape(section)), re.DOTALL)
            if pattern.search(body):
                body, replaced = pattern.subn(block, body), True
                body = body[0]
            else:
                body, replaced = f"{body}\n\n{block}", False
            if len(body) > _MAX_FILE_CHARS:
                raise ValueError("记忆文件超过上限，请先精简既有内容")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body.strip() + "\n", encoding="utf-8")
        return f"已{'更新' if replaced else '新增'}{'用户画像' if scope == 'user' else '课程记忆'}的 {section} 区块"
