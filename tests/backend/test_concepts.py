"""概念抽取：纯规则，同一份教材每次重建得到同样的结果。

概念 id 是掌握度归因的真源，Wiki 页也是按概念一页一页生成的，所以这里抽出什么
直接决定下游两个功能的质量。
"""
from __future__ import annotations

from modules.knowledge.concepts import extract_candidates


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
