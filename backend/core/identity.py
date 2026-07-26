"""用户身份：用户名 → workspace_id。

这不是身份认证：没有密码，知道用户名就能进那个工作区，workspace_id 由用户名哈希得出
因此可推算。本地单机上区分几个人的资料够用，别拿它当访问控制。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

MAX_USERNAME_CHARS = 32
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
