"""概念抽取：纯规则，同一份教材每次重建得到同样的结果。

概念 id 是掌握度归因的真源，Wiki 页也是按概念一页一页生成的，所以这里抽出什么
直接决定下游两个功能的质量。
"""
from __future__ import annotations

from modules.knowledge.concepts import extract_candidates, from_outline, merge_case_variants


def _names(segments: list[tuple[int | None, str]]) -> list[str]:
    return [item["name"] for item in extract_candidates(segments)]


def test_headings_become_concepts():
    names = _names([(1, "# Round Robin\n\n轮转调度把时间片分给每个任务。\n"), (2, "# 上下文切换\n\n切换有开销。\n")])
    assert "Round Robin" in names and "上下文切换" in names


def test_sentences_caught_by_the_numbered_heading_pattern_are_rejected():
    """编号正文行会被当成标题。带逗号或问号的是整句，不是概念名。"""
    segments = [
        (1, "1 And as before, given our new assumptions\n"),
        (2, "2 What can a scheduler do?\n"),
        (3, "3 Shortest Job First\n"),
    ]
    names = _names(segments)
    assert "Shortest Job First" in names
    assert not any("," in name or "?" in name for name in names)


def test_a_short_material_still_yields_concepts():
    """页眉判据在页数太少时会把所有候选都吃掉：一页时任何候选的占比都是 100%。"""
    names = _names([(1, "# 极限\n\n极限描述函数的趋势。\n\n# 连续性\n\n连续性建立在极限之上。\n")])
    assert "极限" in names and "连续性" in names


def test_repeated_running_headers_are_still_dropped_in_a_long_material():
    """反向守护：页数足够时，每页都出现的页眉不该变成概念。"""
    topics = ["极限", "导数", "积分", "级数", "收敛", "梯度", "偏导数", "中值定理"]
    segments = [(number, f"第 3 章 微积分基础\n\n# {topic}\n\n正文。\n") for number, topic in enumerate(topics, start=1)]
    names = _names(segments)
    assert "微积分基础" not in names, "跨页重复的页眉要剔掉"
    assert "极限" in names and "中值定理" in names


def test_formula_fragments_and_urls_are_not_concepts():
    names = _names([
        (1, "# x = y + 1\n"), (2, "# https://example.com/a\n"), (3, "# 批量规范化\n"),
    ])
    assert names == ["批量规范化"]


# ---- 目录书签 ----

def test_outline_beats_scraping_and_drops_section_numbers():
    """书签是作者写的目录，标题里的章节编号要剥掉才是概念名。"""
    rows = [(1, "2  大语言模型基础", 27), (2, "2.1 Transformer结构", 27), (3, "2.1.1 嵌入表示层", 28)]
    names = [item["name"] for item in from_outline(rows)]
    assert names == ["大语言模型基础", "Transformer结构", "嵌入表示层"]


def test_outline_ranks_shallow_entries_first():
    """章 > 节 > 小节。列表按权重倒序取，Wiki 的页数上限才会花在整本书的骨架上。"""
    rows = [(3, "嵌入表示层", 28), (1, "大语言模型基础", 27), (2, "Transformer结构", 27)]
    items = from_outline(rows)
    assert [item["name"] for item in items] == ["大语言模型基础", "Transformer结构", "嵌入表示层"]
    assert items[0]["mention_count"] > items[-1]["mention_count"]


def test_outline_keeps_document_order_within_one_level():
    """同一层按页序，不按字母序——章节顺序本身有意义。"""
    rows = [(1, "优化算法", 453), (1, "引言", 35), (1, "卷积神经网络", 243)]
    assert [item["name"] for item in from_outline(rows)] == ["引言", "卷积神经网络", "优化算法"]


def test_outline_drops_front_matter():
    rows = [(0, "前言", 18), (0, "目 录", 5), (0, "参考文献", 900), (0, "Notation", 30),
            (0, "安装", 27), (0, "注意力机制", 407)]
    assert [item["name"] for item in from_outline(rows)] == ["注意力机制"]


def test_outline_dedupes_repeated_titles_keeping_the_shallowest():
    """d2l 每章都有一个「模型」小节，书签里出现 9 次。同名只留一个，否则列表被它占满。"""
    rows = [(2, "模型", 39), (2, "模型", 156), (2, "模型", 425), (1, "模型", 12), (2, "数据", 38)]
    items = from_outline(rows)
    assert [item["name"] for item in items] == ["模型", "数据"]
    assert items[0]["page"] == 12, "留最浅那一个"


def test_outline_reuses_the_same_sanity_filters_as_scraping():
    rows = [(1, "x = y + 1", 5), (1, "https://example.com", 6), (1, "3.2", 7), (1, "批量规范化", 8)]
    assert [item["name"] for item in from_outline(rows)] == ["批量规范化"]


# ---- 大小写变体 ----

def test_case_variants_merge_and_the_most_mentioned_one_names_the_concept():
    """id 由 casefold 派生，LoRA 与 lora 本来就是同一个概念。"""
    merged = merge_case_variants([
        {"name": "lora", "mention_count": 1, "page": 9},
        {"name": "微调", "mention_count": 4, "page": 2},
        {"name": "LoRA", "mention_count": 6, "page": 3},
    ])
    assert [(item["name"], item["mention_count"], item["page"]) for item in merged] == [
        ("LoRA", 6, 3), ("微调", 4, 2),
    ]


def test_case_variant_merge_is_idempotent_and_breaks_ties_by_order():
    """次数并列时留先出现的；结果再合一次不变，重复索引才拿得到同样的概念。"""
    once = merge_case_variants([{"name": "Attention", "mention_count": 3, "page": 1},
                                {"name": "ATTENTION", "mention_count": 3, "page": 8}])
    assert [item["name"] for item in once] == ["Attention"]
    assert merge_case_variants(once) == once
