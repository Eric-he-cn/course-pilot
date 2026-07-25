from __future__ import annotations

import hashlib
import re

# 标题式候选：Markdown 标题、"第 N 章/节 X"、"7.6 Round Robin" 这类编号标题。
_HEADING_PATTERNS = (
    re.compile(r"^#{1,6}\s+(.{2,60})$", re.MULTILINE),
    re.compile(r"^第\s*[0-9一二三四五六七八九十]+\s*[章节讲]\s*(.{2,40})$", re.MULTILINE),
    re.compile(r"^\d+(?:\.\d+){0,2}\s+([^\n]{2,60})$", re.MULTILINE),
)
# 正文强调式候选：**概念**、「概念」。
_INLINE_PATTERNS = (
    re.compile(r"\*\*([^*\n]{2,30})\*\*"),
    re.compile(r"「([^」\n]{2,30})」"),
)
# 编号、目录点线和符号前后缀，归一化时剥掉。
_STRIP_PREFIX = re.compile(r"^(?:\d+(?:\.\d+)*|[（(]\d+[）)]|[·•\-—.…,，、)）\]】\s]+)+")
_STRIP_SUFFIX = re.compile(r"[\s：:。，,、；;.…\d]+$")
# 公式碎片的判据：教材标题不会带这些运算符。
_MATH_CHARS = re.compile(r"[=+×÷⊤⊥−±≤≥∈∑∏∫√∂∇|^~<>]")
_NOISE = re.compile(r"https?://|@|\d{4}-\d{2}|\.(?:py|js|ts|json|md|txt|sh)$|^\W+$")
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9 ()\-.:'/]+$")
_REPEATED = re.compile(r"(.)\1{3,}")
_MIN_CHARS = 2
_MAX_CHARS = 40
# 出现在这么高比例的页面上，说明是页眉页脚而不是概念。
_HEADER_PAGE_RATIO = 0.35


def _normalize(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()
    text = _STRIP_PREFIX.sub("", text)
    return _STRIP_SUFFIX.sub("", text).strip()


def _acceptable(name: str) -> bool:
    if not (_MIN_CHARS <= len(name) <= _MAX_CHARS):
        return False
    if _NOISE.search(name) or _MATH_CHARS.search(name):
        return False
    if not re.search(r"[一-鿿A-Za-z]", name) or name.isdigit():
        return False
    if _REPEATED.search(name):  # CCCCCCCCCCCCA 这类 PDF 提取乱码
        return False
    # 纯拉丁候选按字母数判断：p(x) 只有两个字母是符号，FIFO/SJF/LoRA 才是概念。
    if _LATIN_ONLY.match(name):
        return len(re.findall(r"[A-Za-z]", name)) >= 3
    return True


def concept_id_for(course_id: str, name: str) -> str:
    """同名概念在同一课程里始终得到同一个 id，重放与增量 diff 都不会改动它。"""
    digest = hashlib.sha1(f"{course_id}\n{name.casefold()}".encode()).hexdigest()[:16]
    return f"concept_{digest}"


def extract_candidates(segments: list[tuple[int | None, str]], *, limit: int = 200) -> list[dict]:
    """从教材文本抽概念候选，按提及次数排序。

    纯规则实现：标题层级与正文强调是教材里最稳定的概念标记，不调用模型，
    因此同一份教材每次重建都得到同样的结果。跨页高频重复的串按页眉页脚剔除。
    """
    counts: dict[str, int] = {}
    buckets: dict[str, set[int]] = {}
    first_page: dict[str, int | None] = {}

    def record(name: str, weight: int, page: int | None, bucket: int) -> None:
        counts[name] = counts.get(name, 0) + weight
        buckets.setdefault(name, set()).add(bucket)
        first_page.setdefault(name, page)

    for index, (page, text) in enumerate(segments):
        bucket = page if page is not None else index
        for pattern in _HEADING_PATTERNS:
            for match in pattern.finditer(text):
                name = _normalize(match.group(1))
                if _acceptable(name):
                    record(name, 3, page, bucket)  # 标题权重高于正文强调
        for pattern in _INLINE_PATTERNS:
            for match in pattern.finditer(text):
                name = _normalize(match.group(1))
                if _acceptable(name):
                    record(name, 1, page, bucket)

    total_buckets = len({page if page is not None else index for index, (page, _) in enumerate(segments)}) or 1
    ranked = sorted(
        ((name, count) for name, count in counts.items() if len(buckets[name]) / total_buckets <= _HEADER_PAGE_RATIO),
        key=lambda item: (-item[1], item[0]),
    )
    return [{"name": name, "mention_count": count, "page": first_page.get(name)} for name, count in ranked[:limit]]
