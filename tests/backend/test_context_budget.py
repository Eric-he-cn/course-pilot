"""上下文预算的口径：预算量的是估算 token，不是字符数。"""
from __future__ import annotations

from modules.agent.context import assemble_general_messages, estimate_tokens


def _kept_history(history, budget: int) -> list[str]:
    """assemble_general_messages 的消息是 [system, *历史, 当前问题]。"""
    assembled = assemble_general_messages(
        courses=["算法"], history=history, question="q", history_token_budget=budget,
    )
    return [item.content for item in assembled.messages[1:-1]]


def test_estimate_separates_cjk_from_latin():
    """同样长度的中文与英文，估出来的 token 数应当差三四倍。"""
    latin = "the quick brown fox jumps over a lazy dog " * 20
    cjk = "敏捷的棕色狐狸跳过了那只懒狗" * 60
    cjk = cjk[:len(latin)]

    assert len(cjk) == len(latin)
    ratio = estimate_tokens(cjk) / estimate_tokens(latin)
    assert 3.0 <= ratio <= 4.0, f"中英倍率 {ratio:.2f} 不在三到四倍之间"
    # 纯英文与「字符数 ÷ 3.5~4」同量级
    assert len(latin) / 4 <= estimate_tokens(latin) <= len(latin) / 3.0


def test_estimate_never_underestimates_pure_cjk():
    """中文按 1 字 1 token 折算：宁可高估，低估会顶爆上游窗口。"""
    text = "操作系统的进程调度" * 100
    assert estimate_tokens(text) >= len(text)


def test_same_budget_keeps_less_chinese_than_english_history():
    """同一预算下，中文历史能保住的字符数明显少于英文——这正是按 token 计量的效果。"""
    budget = 4_000
    chinese = [(role, "调度算法的护航效应说明短作业会被长作业堵住。" * 4) for role in ("user", "assistant")] * 40
    english = [(role, "Convoy effect means short jobs get stuck behind a long one. " * 4) for role in ("user", "assistant")] * 40

    chinese_chars = sum(len(text) for text in _kept_history(chinese, budget))
    english_chars = sum(len(text) for text in _kept_history(english, budget))

    assert chinese_chars > 0 and english_chars > 0
    assert english_chars >= chinese_chars * 2.5, f"英文 {english_chars} 字符 vs 中文 {chinese_chars} 字符"


def test_history_budget_is_counted_in_tokens():
    """留下来的历史，其估算 token 数要贴着预算，而不是贴着字符数。"""
    budget = 4_000
    english = [(role, "Convoy effect means short jobs get stuck behind a long one. " * 4) for role in ("user", "assistant")] * 40

    kept = _kept_history(english, budget)
    tokens = sum(estimate_tokens(text) for text in kept)
    chars = sum(len(text) for text in kept)

    assert tokens <= budget
    assert tokens > budget * 0.8, f"预算 {budget} 只用掉 {tokens} token，历史被白丢了"
    assert chars > budget * 2, f"英文历史只放进 {chars} 字符，说明预算还在按字符裁"
