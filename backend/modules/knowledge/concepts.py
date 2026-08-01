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
# 整句的迹象：逗号、问号、句中的分号。概念名不会有这些。
_SENTENCE_LIKE = re.compile(r"[,，;；?？!！]")
_REPEATED = re.compile(r"(.)\1{3,}")
_MIN_CHARS = 2
_MAX_CHARS = 40
# 出现在这么高比例的页面上，说明是页眉页脚而不是概念。
_HEADER_PAGE_RATIO = 0.35
# 页数少于这个数就不做页眉判定，否则短资料的概念会被全部剔掉。
_HEADER_MIN_BUCKETS = 5


def _normalize(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()
    text = _STRIP_PREFIX.sub("", text)
    return _STRIP_SUFFIX.sub("", text).strip()


def _acceptable(name: str) -> bool:
    if not (_MIN_CHARS <= len(name) <= _MAX_CHARS):
        return False
    # 概念名是名词短语。带逗号或问号的是被编号正文行误判成标题的整句
    # （「1 And as before, given our new assumptions」这种），不是概念。
    if _SENTENCE_LIKE.search(name):
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


# 书签标题里的章节编号很规整（"2.1.1 嵌入表示层"），剥掉它才是概念名。
# 与 _STRIP_PREFIX 分开：那个会把「16 位浮点数」的 16 也啃掉，只在刮正文时用。
_OUTLINE_SECTION_NO = re.compile(
    r"^(?:\d+(?:\.\d+)*|[IVXLC]+|第\s*[0-9一二三四五六七八九十]+\s*[章节讲部篇])[.、\s]+"
)
# 前言目录索引这类不是概念
_FRONT_MATTER = re.compile(
    r"^(前沿|前言|序言?|目\s*录|索\s*引|附\s*录|参考文献|数学符号|符号|致谢|版权|后记|扉页|安装|环境配置"
    r"|preface|foreword|contents|index|bibliography|references|notation|appendix"
    r"|acknowledge?ments?|about\s+the\s+authors?|installation|colophon)\b",
    re.IGNORECASE,
)
# 层级换成权重：越浅越靠前。列表按 mention_count 倒序取，所以整本书的章节会排在细节小节之前。
_OUTLINE_MAX_LEVEL_WEIGHT = 10


def from_outline(rows: list[tuple[int, str, int | None]], *, limit: int = 200) -> list[dict]:
    """把目录书签整理成概念候选。同名只留最浅、最靠前那一个。"""
    seen: dict[str, dict] = {}
    for level, title, page in sorted(rows, key=lambda row: (row[0], row[2] if row[2] is not None else 0)):
        name = _normalize_outline(title)
        if not name or _FRONT_MATTER.match(name) or not _acceptable(name):
            continue
        seen.setdefault(name, {
            "name": name,
            "mention_count": max(1, _OUTLINE_MAX_LEVEL_WEIGHT - min(level, _OUTLINE_MAX_LEVEL_WEIGHT - 1)),
            "page": page,
        })
    ordered = sorted(seen.values(), key=lambda item: (-item["mention_count"], item["page"] or 0, item["name"]))
    return ordered[:limit]


def _normalize_outline(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw).strip()
    text = _OUTLINE_SECTION_NO.sub("", text).strip()
    return text.strip(" .·—-:：")


def concept_id_for(course_id: str, name: str) -> str:
    """同名概念在同一课程里始终得到同一个 id，重放与增量 diff 都不会改动它。"""
    digest = hashlib.sha1(f"{course_id}\n{name.casefold()}".encode()).hexdigest()[:16]
    return f"concept_{digest}"


def merge_case_variants(candidates: list[dict]) -> list[dict]:
    """把只差大小写的候选合成一个：id 由 casefold 派生，它们本来就是同一个概念。

    显示名与页码取提及次数最多的那个变体，并列时取先出现的；次数本身取较大值，
    与同名概念跨教材 upsert 的口径一致。候选顺序确定，所以重复索引结果不变。
    """
    merged: dict[str, dict] = {}
    for candidate in candidates:
        key = candidate["name"].casefold()
        winner = merged.get(key)
        if winner is None or candidate.get("mention_count", 1) > winner.get("mention_count", 1):
            merged[key] = candidate
    return list(merged.values())


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
    # 页眉判据在页数太少时会把所有概念都吃掉：只有 1 页时任何候选的占比都是 100%。
    # 页数少于这个数就不判页眉——几页的资料本来也没有页眉重复的问题。
    header_filter = total_buckets >= _HEADER_MIN_BUCKETS
    ranked = sorted(
        (
            (name, count) for name, count in counts.items()
            if not header_filter or len(buckets[name]) / total_buckets <= _HEADER_PAGE_RATIO
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return [{"name": name, "mention_count": count, "page": first_page.get(name)} for name, count in ranked[:limit]]
