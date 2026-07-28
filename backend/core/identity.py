"""用户身份：用户名 → workspace_id。

这不是身份认证：没有密码，知道用户名就能进那个工作区，workspace_id 由用户名哈希得出
因此可推算。本地单机上区分几个人的资料够用，别拿它当访问控制。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

MAX_USERNAME_CHARS = 32

# 一个用户工作区包含的东西。新增按用户隔离的目录必须登记在这里，否则从旧布局
# 迁移时会把它落在原地——数据还在，但新工作区读不到，而且没有任何报错。
# 库要连 WAL 的边车文件一起算：只搬主文件会丢掉已提交事务的尾部。
WORKSPACE_ITEMS = (
    "coursepilot.db", "coursepilot.db-wal", "coursepilot.db-shm",
    "materials", "notes", "wiki", "traces", "courses", "user.md",
)
# 判断某个目录是不是旧布局：只认必然存在的那几项，缺了笔记或 Wiki 也算。
LEGACY_MARKERS = ("coursepilot.db", "materials", "traces")
# 白名单：中日韩文字、字母、数字与 - _ 空格。零宽连接符与双向控制符都是 Cf 类，
# 不在这个集合里，会被自然拒掉。
_ALLOWED = re.compile(r"^[\w　-鿿぀-ヿ가-힯 \-]+$", re.UNICODE)


class InvalidUsername(ValueError):
    pass


def normalize_username(raw: str) -> str:
    """先折叠大小写再归一化：casefold 本身不保证保持 NFC。"""
    text = unicodedata.normalize("NFC", str(raw or "").strip().casefold())
    if not text:
        raise InvalidUsername("用户名不能为空")
    if len(text) > MAX_USERNAME_CHARS:
        # 静默截断会把人塞进别人的工作区，所以超限直接拒绝。
        raise InvalidUsername(f"用户名不能超过 {MAX_USERNAME_CHARS} 个字符")
    if not _ALLOWED.match(text):
        raise InvalidUsername("用户名只能包含中日韩文字、字母、数字、空格与 - _")
    return text


def workspace_id(username: str) -> str:
    """哈希让用户名永远不参与路径，任何怪名字都不可能穿越目录。"""
    digest = hashlib.sha1(normalize_username(username).encode("utf-8")).hexdigest()[:16]
    return f"user_{digest}"


def sole_workspace(data_dir) -> Path:
    """脚本用：定位 <data>/users/ 下唯一的工作区。

    直接拼 <data>/coursepilot.db 的话 sqlite3.connect 会凭空建一个空库，
    之后报的是「no such table」，极难看出真正的原因。
    """
    root = Path(data_dir)
    # 也接受直接指向某个工作区，否则「多个工作区」的提示照做一遍还是找不到。
    if (root / "coursepilot.db").is_file():
        return root
    candidates = sorted(root.glob("users/*/coursepilot.db"))
    if not candidates:
        raise SystemExit(f"{data_dir} 下没有工作区。先跑一次应用建数据，或用 --data-dir 指到对的地方。")
    if len(candidates) > 1:
        listed = "\n".join(f"  --data-dir {path.parent}" for path in candidates)
        raise SystemExit(f"{data_dir} 下有多个工作区，用下面任意一行指定：\n{listed}")
    return candidates[0].parent
