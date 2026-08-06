#!/usr/bin/env python
"""多教材 Wiki e2e 评测 · 离线判据。全确定性，不用 LLM judge。

    .venv/bin/python scripts/e2e_wiki/judge.py --dataset testdata/e2e-wiki/e2e_wiki_dataset.yaml \
        --jsonl R=testdata/e2e-wiki/out/R.jsonl --jsonl W=testdata/e2e-wiki/out/W.jsonl \
        [--detail] [--json testdata/e2e-wiki/out/judge.json]
    .venv/bin/python scripts/e2e_wiki/judge.py --self-test        # 判据自己的 A/B，离线不花钱

四个维度，都落在「用户能感知的性质」上，不落在条目数、措辞、发生在第几轮这些实现细节上：

1. must_contain —— 归一化后逐条 re.search，全部命中才算过。归一化口径见 common.normalize
   （NFKC → 部首折叠 → 剔 LaTeX 与代码 → 压空白），与题目集头部的约定同一套。

2. attribution —— 教材引用（kind=material）的 (document, page) 落在本题标定的页集合里算 hit，
   落在集合外算 miss，miss 再分两种：off_doc（引到本题不相关的教材）、off_page（教材对、页不对）。
   知识页引用（kind=wiki）单独计数，永远不算 miss——它没有页码，按设计就核对不到页。
   两个口径都报：
     cited     —— 正文里真标了 [n] 的那些。这是用户点得开的依据，主口径。
     retrieved —— 本轮登记过的全部引用。模型不标编号时 cited 会空，那时看这个才知道是
                  「没检索到」还是「检索到了没标」。

3. conflate_pairs —— a、b 两串（squash 口径：NFKC + 部首折叠 + 去掉所有空白）同时出现在回答里，
   且回答里找不到对照说明 → 记 conflated。分母只算「两串真的同现」的那些轮次：
   模型压根没提第二本书的写法时，它没有混淆，也没有对照，两种都不该算进这个比率。

   对照说明词表分两档，因为一档搜不出来：
     STRONG（整段搜）—— 「两本」「不同教材」「另一份」、直接点名出处等，出现在回答任何位置
       都足以说明作者知道这里有两套东西。
     WEAK（只在两串所在的窗口里搜）—— 「分别」「各自」「前者」「记作」这类。它们在中文里
       太常见（「太大和太小分别会…」随处可见），整段搜等于把这条判据变成恒绿；只有紧贴着
       那两串出现时才是在做对照。窗口是 squash 口径下两串最近一次同现的跨度各外扩
       WEAK_WINDOW 个字符。
   both_ok_if 那段散文是给人看的，机器判不了，只在明细里带出来。

4. single_source —— 教材引用是否命中标定页。它判的是「wiki 有没有把教材席位挤掉」：
   wiki_displaced = 有知识页引用、但一条教材引用都没落在标定页上。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import normalize, squash  # noqa: E402

# 整段搜就够的强信号：说出「这里有两套」或直接点名出处，读者就不会把两套并成一套。
STRONG_CONTRAST = (
    "两本", "两份", "两种记", "两套", "两处", "两个教材", "两本教材", "两份教材", "两种写法",
    "两种约定", "两种口径", "两种数法",
    "不同教材", "不同的教材", "不同书", "不同的书", "不同资料", "不同的资料", "不同笔记",
    "另一本", "另一份", "另一套", "有的教材", "有些教材", "有的书", "有些资料", "有的笔记",
    "约定不同", "口径不同", "定义不同", "说法不同", "数法不同", "记号不同", "符号不同",
    "取决于教材", "视教材", "因教材而异",
    # 直接点名出处：把话说到哪本书上就是最明确的对照
    "ml-notes", "dl-notes", "d2l", "happy-llm", "吴恩达", "动手学深度学习",
    "机器学习笔记", "深度学习笔记",
)
# 弱信号：中文里太常见，整段搜会让这条判据恒绿，只在两串所在的窗口里才算。
WEAK_CONTRAST = (
    "各自", "分别", "前者", "后者", "写作", "写成", "记作", "记为", "写法", "记号",
    "符号约定", "对应地", "相应地", "而在", "则在", "这里用", "那里用",
)
# 窗口半径，squash 口径（无空白）下的字符数。两串同现的最近跨度各外扩这么多。
WEAK_WINDOW = 120


# ── 单轮判定 ──────────────────────────────────────────────────────────────────

def _occurrences(haystack: str, needle: str) -> list[int]:
    found, start = [], haystack.find(needle)
    while start >= 0:
        found.append(start)
        start = haystack.find(needle, start + 1)
    return found


def contrast_markers(answer_squash: str, a: str, b: str, *,
                     strong: tuple[str, ...] = STRONG_CONTRAST,
                     weak: tuple[str, ...] = WEAK_CONTRAST) -> list[str]:
    """回答里说明「这是两套不同写法」的证据。空列表表示两串同现却没做任何对照。"""
    lowered = answer_squash.lower()
    markers = [word for word in strong if word.lower() in lowered]
    a_at, b_at = _occurrences(answer_squash, a), _occurrences(answer_squash, b)
    if not a_at or not b_at:
        return markers
    # 两串最近一次同现的跨度，各外扩一个窗口——弱信号只有落在这里面才是在做对照
    left, right = min(
        ((min(x, y), max(x + len(a), y + len(b))) for x in a_at for y in b_at),
        key=lambda span: span[1] - span[0],
    )
    window = lowered[max(0, left - WEAK_WINDOW): right + WEAK_WINDOW]
    markers += [f"~{word}" for word in weak if word.lower() in window]
    return markers


def judge_record(record: dict, sample: dict,
                 contrast: tuple[str, ...] | None = None) -> dict:
    answer_norm = normalize(record.get("answer_text") or "")
    answer_squash = squash(record.get("answer_text") or "")

    patterns = []
    for item in sample.get("must_contain") or []:
        hit = bool(re.search(item["pattern"], answer_norm, re.S))
        patterns.append({"pattern": item["pattern"], "note": item.get("note"), "hit": hit})
    must_pass = bool(patterns) and all(item["hit"] for item in patterns)

    allowed: dict[str, set[int]] = {}
    for item in sample.get("attribution") or []:
        allowed.setdefault(item["document"], set()).update(item["pages"])

    citations = record.get("citations") or []
    attribution = {scope: _attribution(citations, allowed, cited_only=(scope == "cited"))
                   for scope in ("cited", "retrieved")}

    strong = contrast if contrast is not None else STRONG_CONTRAST
    pairs = []
    for pair in sample.get("conflate_pairs") or []:
        a, b = squash(str(pair["a"])), squash(str(pair["b"]))
        a_in, b_in = a in answer_squash, b in answer_squash
        markers = contrast_markers(answer_squash, a, b, strong=strong) if (a_in and b_in) else []
        pairs.append({
            "a_doc": pair.get("a_doc"), "b_doc": pair.get("b_doc"),
            "a_in": a_in, "b_in": b_in, "co_occur": a_in and b_in,
            "contrast_markers": markers[:6],
            "conflated": bool(a_in and b_in and not markers),
            "both_ok_if": pair.get("both_ok_if"),
        })

    wiki_count = attribution["retrieved"]["wiki"]
    return {
        "sample_id": sample["id"], "kind": sample["kind"], "topic": sample.get("topic"),
        "arm": record.get("arm"), "run": record.get("run"), "ok": bool(record.get("ok")),
        "error": record.get("error"),
        "answer_chars": len(record.get("answer_text") or ""),
        "must_contain": {"pass": must_pass, "hits": sum(1 for p in patterns if p["hit"]),
                         "total": len(patterns), "patterns": patterns},
        "attribution": attribution,
        "conflate": {"pairs": pairs,
                     "co_occur": sum(1 for p in pairs if p["co_occur"]),
                     "conflated": sum(1 for p in pairs if p["conflated"])},
        # single_source 专用：知识页来了、教材一条都没落在标定页上，就是席位被挤掉了
        "wiki_displaced": bool(sample["kind"] == "single_source" and wiki_count > 0
                               and attribution["retrieved"]["hit"] == 0),
        "tool_calls": record.get("tool_calls") or [],
        "wiki_evidence_tokens": _wiki_tokens(record),
    }


def _attribution(citations: list[dict], allowed: dict[str, set[int]], *, cited_only: bool) -> dict:
    picked = [item for item in citations if not cited_only or item.get("cited")]
    material = [item for item in picked if item.get("kind") == "material"]
    hit, off_doc, off_page = [], [], []
    for item in material:
        pages = allowed.get(item.get("document") or "")
        if pages is None:
            off_doc.append(item)
        elif item.get("page") in pages:
            hit.append(item)
        else:
            off_page.append(item)
    total = len(material)
    return {
        "material": total,
        "wiki": sum(1 for item in picked if item.get("kind") == "wiki"),
        "web": sum(1 for item in picked if item.get("kind") == "web"),
        "hit": len(hit), "off_doc": len(off_doc), "off_page": len(off_page),
        "miss": len(off_doc) + len(off_page),
        "pass": len(hit) > 0,
        "precision": (len(hit) / total) if total else None,
        # 命中了几份不同的教材：cross_source 题目的重点是别只从一本书取证
        "hit_documents": sorted({item.get("document") for item in hit}),
        "documents": sorted({item.get("document") for item in material if item.get("document")}),
    }


def _wiki_tokens(record: dict) -> int | None:
    """本轮上下文里知识页正文占了多少 token。

    context_usage 事件只报 tokens>0 的分段，所以事件在、这一段不在时它就是 0；
    整个事件都没有才是 None（拿不到数据，不能当成 0）。
    """
    context = record.get("context_usage")
    if not context:
        return None
    for segment in context.get("segments") or []:
        if segment.get("label_key") == "context.segment.wiki_evidence":
            return segment.get("tokens")
    return 0


# ── 汇总 ──────────────────────────────────────────────────────────────────────

def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole * 100:4.0f}%)" if whole else f"{part}/0 ( n/a)"


def summarize(verdicts: list[dict], scope: str = "cited") -> dict:
    def block(rows: list[dict]) -> dict:
        ok = [row for row in rows if row["ok"]]
        attr = [row["attribution"][scope] for row in ok]
        hits = sum(item["hit"] for item in attr)
        miss = sum(item["miss"] for item in attr)
        co = sum(row["conflate"]["co_occur"] for row in ok)
        return {
            "turns": len(rows), "ok": len(ok), "failed": len(rows) - len(ok),
            "must_pass": sum(1 for row in ok if row["must_contain"]["pass"]),
            "must_patterns_hit": sum(row["must_contain"]["hits"] for row in ok),
            "must_patterns_total": sum(row["must_contain"]["total"] for row in ok),
            "attr_pass": sum(1 for item in attr if item["pass"]),
            "attr_hits": hits, "attr_miss": miss,
            "attr_off_doc": sum(item["off_doc"] for item in attr),
            "attr_off_page": sum(item["off_page"] for item in attr),
            "attr_precision": (hits / (hits + miss)) if (hits + miss) else None,
            "multi_doc": sum(1 for item in attr if len(item["hit_documents"]) >= 2),
            "material_citations": sum(item["material"] for item in attr),
            "wiki_citations": sum(item["wiki"] for item in attr),
            "turns_with_wiki": sum(1 for item in attr if item["wiki"] > 0),
            "conflate_co_occur": co,
            "conflated": sum(row["conflate"]["conflated"] for row in ok),
            "wiki_displaced": sum(1 for row in ok if row["wiki_displaced"]),
            "tools": Counter(name for row in ok for name in row["tool_calls"]),
        }

    return {
        "scope": scope,
        "all": block(verdicts),
        "cross_source": block([row for row in verdicts if row["kind"] == "cross_source"]),
        "single_source": block([row for row in verdicts if row["kind"] == "single_source"]),
    }


def _width(text: str) -> int:
    """终端显示宽度：中日韩字符占两列，按字符数补空格会让表格错位。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, width: int, *, right: bool = False) -> str:
    fill = " " * max(0, width - _width(text))
    return (fill + text) if right else (text + fill)


ARM_ROWS: list[tuple[str, object]] = [
    ("轮次（成功/全部）", lambda b: f"{b['ok']}/{b['turns']}"),
    ("must_contain 题均通过", lambda b: _rate(b["must_pass"], b["ok"])),
    ("must_contain 锚点命中", lambda b: _rate(b["must_patterns_hit"], b["must_patterns_total"])),
    ("attribution 轮均命中", lambda b: _rate(b["attr_pass"], b["ok"])),
    ("attribution 引用精确", lambda b: (f"{b['attr_hits']}/{b['attr_hits'] + b['attr_miss']}"
                                        f" ({b['attr_precision'] * 100:.0f}%)"
                                        if b["attr_precision"] is not None else "n/a")),
    ("  其中 off_doc/off_page", lambda b: f"{b['attr_off_doc']}/{b['attr_off_page']}"),
    ("取证跨 ≥2 份教材", lambda b: _rate(b["multi_doc"], b["ok"])),
    ("教材引用条数", lambda b: str(b["material_citations"])),
    ("知识页引用条数", lambda b: str(b["wiki_citations"])),
    ("有知识页引用的轮次", lambda b: _rate(b["turns_with_wiki"], b["ok"])),
    ("conflate 同现轮次", lambda b: str(b["conflate_co_occur"])),
    ("  其中判为混淆", lambda b: _rate(b["conflated"], b["conflate_co_occur"])),
    ("知识页挤掉教材席位", lambda b: _rate(b["wiki_displaced"], b["ok"])),
]


def print_arm(label: str, summary: dict) -> None:
    print(f"\n=== 臂 {label} · 口径 {summary['scope']} ===")
    name_width, cell = 26, 18
    print(pad("维度", name_width) + pad("全部", cell, right=True)
          + pad("cross_source", cell, right=True) + pad("single_source", cell, right=True))
    print("-" * (name_width + cell * 3))
    for name, getter in ARM_ROWS:
        print(pad(name, name_width)
              + "".join(pad(getter(summary[group]), cell, right=True)
                        for group in ("all", "cross_source", "single_source")))
    tools = summary["all"]["tools"]
    print("工具调用：" + ("、".join(f"{name}×{count}" for name, count in tools.most_common()) or "（无）"))


COMPARE_ROWS: list[tuple[str, object]] = [
    ("must_contain 通过率", lambda b: (b["must_pass"], b["ok"])),
    ("attribution 命中率", lambda b: (b["attr_pass"], b["ok"])),
    ("取证跨 ≥2 份教材", lambda b: (b["multi_doc"], b["ok"])),
    ("知识页引用轮次占比", lambda b: (b["turns_with_wiki"], b["ok"])),
    ("混淆率（同现为分母）", lambda b: (b["conflated"], b["conflate_co_occur"])),
]


def print_compare(summaries: dict[str, dict]) -> None:
    labels = list(summaries)
    print(f"\n=== 各臂对比（口径 {summaries[labels[0]]['scope']}）===")
    print("Δ 是最后一臂减第一臂的百分点差；分母为 0 的格子报 n/a，不当成 0。")
    name_width, cell = 24, 18
    for group in ("all", "cross_source", "single_source"):
        print(f"\n-- {group} --")
        print(pad("", name_width) + "".join(pad(label, cell, right=True) for label in labels)
              + pad("Δ", 12, right=True))
        for name, getter in COMPARE_ROWS:
            cells, values = [], []
            for label in labels:
                part, whole = getter(summaries[label][group])
                cells.append(_rate(part, whole))
                values.append((part / whole) if whole else None)
            delta = ("n/a" if len(values) < 2 or values[0] is None or values[-1] is None
                     else f"{(values[-1] - values[0]) * 100:+.0f}pp")
            print(pad(name, name_width) + "".join(pad(text, cell, right=True) for text in cells)
                  + pad(delta, 12, right=True))


def print_detail(label: str, verdicts: list[dict], scope: str) -> None:
    print(f"\n=== 臂 {label} · 每题明细（口径 {scope}）===")
    print(pad("id", 10) + pad("kind", 15) + pad("must", 8, right=True)
          + pad("attr", 8, right=True) + pad("混淆", 8, right=True)
          + pad("docs", 7, right=True) + pad("wiki", 7, right=True) + "  备注")
    by_sample: dict[str, list[dict]] = {}
    for row in verdicts:
        by_sample.setdefault(row["sample_id"], []).append(row)
    for sample_id, rows in by_sample.items():
        ok = [row for row in rows if row["ok"]]
        attr = [row["attribution"][scope] for row in ok]
        co = sum(row["conflate"]["co_occur"] for row in ok)
        note = []
        if len(ok) < len(rows):
            note.append(f"{len(rows) - len(ok)} 轮失败")
        missing = sorted({item["note"] or item["pattern"]
                          for row in ok for item in row["must_contain"]["patterns"] if not item["hit"]})
        if missing:
            note.append("漏锚点：" + "；".join(text[:28] for text in missing[:2]))
        if any(row["wiki_displaced"] for row in ok):
            note.append("知识页挤掉教材席位")
        print(pad(sample_id, 10) + pad(rows[0]["kind"], 15)
              + pad(f"{sum(1 for row in ok if row['must_contain']['pass'])}/{len(ok)}", 8, right=True)
              + pad(f"{sum(1 for item in attr if item['pass'])}/{len(ok)}", 8, right=True)
              + pad(f"{sum(row['conflate']['conflated'] for row in ok)}/{co}", 8, right=True)
              + pad(str(max((len(item["hit_documents"]) for item in attr), default=0)), 7, right=True)
              + pad(str(sum(item["wiki"] for item in attr)), 7, right=True)
              + "  " + " · ".join(note))


# ── 自检 ──────────────────────────────────────────────────────────────────────

def self_test() -> int:
    """判据自己的 A/B：三条假记录，判定必须与预期逐项相符。"""
    sample = {
        "id": "t-001", "kind": "cross_source", "topic": "反向传播",
        "must_contain": [{"pattern": "输出层", "note": "从输出层往回算"},
                         {"pattern": "(缓存|存下)", "note": "正向要缓存"}],
        "attribution": [{"document": "ml-notes-slice.pdf", "pages": [53, 54]},
                        {"document": "dl-notes-slice.pdf", "pages": [3, 4]}],
        "conflate_pairs": [{"a": "δ(3)=(Θ(3))Tδ(4)", "a_doc": "ml-notes-slice.pdf",
                            "b": "dZ[l]=dA[l]", "b_doc": "dl-notes-slice.pdf",
                            "both_ok_if": "说明两套记号各属哪本"}],
    }
    # 一条全中：锚点齐、引用落在标定页、两套记号同现但明确交代了各属哪本
    good = {
        "arm": "T", "run": 1, "ok": True,
        "answer_text": ("误差从输出层往回算 [1]，正向那一遍要把 z 缓存下来 [2]。\n"
                        "两本教材记号不同：ml-notes 写作 δ(3) = (Θ(3))T δ(4)，"
                        "dl-notes 写作 dZ[l] = dA[l]，分别对应各自的层号约定。"),
        "citations": [
            {"number": 1, "kind": "material", "document": "ml-notes-slice.pdf", "page": 53, "cited": True},
            {"number": 2, "kind": "material", "document": "dl-notes-slice.pdf", "page": 4, "cited": True},
        ],
        "tool_calls": ["search_materials"],
        "context_usage": {"segments": [{"label_key": "context.segment.wiki_evidence", "tokens": 640}]},
    }
    # 一条混淆：两套记号同现，一个对照说明词都没有
    conflated = {
        "arm": "T", "run": 1, "ok": True,
        "answer_text": ("误差从输出层往回算 [1]，正向要缓存 z。"
                        "逐层的公式是 δ(3) = (Θ(3))T δ(4)，也就是 dZ[l] = dA[l]。"),
        "citations": [
            {"number": 1, "kind": "material", "document": "ml-notes-slice.pdf", "page": 54, "cited": True},
        ],
        "tool_calls": ["search_materials"],
        "context_usage": {"segments": []},
    }
    # 一条 attribution miss：教材对、页不对 + 引到本题不相关的教材
    miss = {
        "arm": "T", "run": 1, "ok": True,
        # 刻意不写「缓存 / 存下」：第二条锚点要如期落空，否则这条记录只测到 attribution
        "answer_text": "误差从输出层往回算 [1][2]，正向那一遍把中间量记在一边。",
        "citations": [
            {"number": 1, "kind": "material", "document": "ml-notes-slice.pdf", "page": 12, "cited": True},
            {"number": 2, "kind": "material", "document": "d2l-slice.pdf", "page": 7, "cited": True},
            {"number": 3, "kind": "wiki", "concept_id": "section_x", "concept_name": "反向传播",
             "page": None, "cited": False},
        ],
        "tool_calls": ["search_materials"],
        "context_usage": None,
    }

    checks: list[tuple[str, bool, str]] = []

    def expect(name: str, got, want) -> None:
        checks.append((name, got == want, f"得到 {got!r}，预期 {want!r}"))

    a = judge_record(good, sample)
    expect("全中·must_contain 通过", a["must_contain"]["pass"], True)
    expect("全中·两条锚点都命中", a["must_contain"]["hits"], 2)
    expect("全中·attribution 通过", a["attribution"]["cited"]["pass"], True)
    expect("全中·hit 两条", a["attribution"]["cited"]["hit"], 2)
    expect("全中·miss 零条", a["attribution"]["cited"]["miss"], 0)
    expect("全中·取证跨两份教材", len(a["attribution"]["cited"]["hit_documents"]), 2)
    expect("全中·两串同现", a["conflate"]["co_occur"], 1)
    expect("全中·有对照说明所以不算混淆", a["conflate"]["conflated"], 0)
    expect("全中·知识页 token 读到了", a["wiki_evidence_tokens"], 640)

    b = judge_record(conflated, sample)
    expect("混淆·两串同现", b["conflate"]["co_occur"], 1)
    expect("混淆·判为 conflated", b["conflate"]["conflated"], 1)
    expect("混淆·must_contain 仍通过", b["must_contain"]["pass"], True)
    expect("混淆·attribution 仍通过", b["attribution"]["cited"]["pass"], True)
    expect("混淆·事件在但无该段则记 0", b["wiki_evidence_tokens"], 0)

    c = judge_record(miss, sample)
    expect("miss·attribution 不通过", c["attribution"]["cited"]["pass"], False)
    expect("miss·hit 零条", c["attribution"]["cited"]["hit"], 0)
    expect("miss·off_page 一条", c["attribution"]["cited"]["off_page"], 1)
    expect("miss·off_doc 一条", c["attribution"]["cited"]["off_doc"], 1)
    expect("miss·精确率 0", c["attribution"]["cited"]["precision"], 0.0)
    expect("miss·漏掉「缓存」那条锚点", c["must_contain"]["pass"], False)
    expect("miss·知识页只进 retrieved 口径", c["attribution"]["retrieved"]["wiki"], 1)
    expect("miss·知识页不进 cited 口径", c["attribution"]["cited"]["wiki"], 0)
    expect("miss·没有 context_usage 时记 None", c["wiki_evidence_tokens"], None)

    # 两档词表：弱信号离得远不算对照，贴着两串才算；强信号在任何位置都算。
    far = ("误差从输出层往回算，正向要缓存 z。学习率太大和太小分别会带来不收敛和收敛慢两种后果。"
           + "补充说明。" * 30
           + "逐层公式是 δ(3) = (Θ(3))T δ(4)，也就是 dZ[l] = dA[l]。")
    near = ("误差从输出层往回算，正向要缓存 z。"
            "逐层公式分别写作 δ(3) = (Θ(3))T δ(4) 与 dZ[l] = dA[l]。")
    strong_only = ("误差从输出层往回算，正向要缓存 z。"
                   "δ(3) = (Θ(3))T δ(4) 与 dZ[l] = dA[l] 来自不同教材。")
    expect("弱信号离得远 → 仍判混淆",
           judge_record({**conflated, "answer_text": far}, sample)["conflate"]["conflated"], 1)
    expect("弱信号贴着两串 → 不判混淆",
           judge_record({**conflated, "answer_text": near}, sample)["conflate"]["conflated"], 0)
    expect("强信号在任何位置 → 不判混淆",
           judge_record({**conflated, "answer_text": strong_only}, sample)["conflate"]["conflated"], 0)

    # 归一化本身：部首折叠与剔公式这两件事必须真生效，否则上面的判据都建在沙上
    expect("部首折叠：⻓度 → 长度", "长度" in normalize("序列⻓度"), True)
    expect("剔代码块：围栏里的词不算正文", "softmax" in normalize("见下\n```\nsoftmax(x)\n```\n"), False)
    expect("剔行内公式：$...$ 里的词不算正文", "lambda" in normalize("正则项 $\\lambda w^2$ 加上去"), False)
    expect("squash 去空白后能匹配跨行公式", squash("dZ[l]\n= dA[l]"), "dZ[l]=dA[l]")

    # 汇总层：三条记录的比率要算得对
    summary = summarize([a, b, c])
    expect("汇总·must 通过 2/3", (summary["all"]["must_pass"], summary["all"]["ok"]), (2, 3))
    expect("汇总·attr 通过 2/3", (summary["all"]["attr_pass"], summary["all"]["ok"]), (2, 3))
    expect("汇总·混淆 1/2（分母只算同现）",
           (summary["all"]["conflated"], summary["all"]["conflate_co_occur"]), (1, 2))
    expect("汇总·cross 分组拿到 3 轮", summary["cross_source"]["turns"], 3)
    expect("汇总·single 分组是空的", summary["single_source"]["turns"], 0)

    # single_source 的挤席位判据
    single = {**sample, "id": "t-002", "kind": "single_source",
              "attribution": [{"document": "dl-notes-slice.pdf", "pages": [63]}],
              "conflate_pairs": []}
    displaced = judge_record({**miss, "citations": [
        {"number": 1, "kind": "wiki", "concept_id": "s", "concept_name": "CNN", "page": None, "cited": True},
    ]}, single)
    expect("single·只有知识页 → 判为挤掉席位", displaced["wiki_displaced"], True)
    kept = judge_record({**miss, "citations": [
        {"number": 1, "kind": "material", "document": "dl-notes-slice.pdf", "page": 63, "cited": True},
        {"number": 2, "kind": "wiki", "concept_id": "s", "concept_name": "CNN", "page": None, "cited": True},
    ]}, single)
    expect("single·教材命中了就不算挤掉", kept["wiki_displaced"], False)

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f" — {detail}"))
    # 条数闸：跳过分支会缩小分母，让「全过」看着像成功
    expected = 37
    print(f"\n{passed}/{len(checks)} 通过（预期 {expected} 条判据）")
    if len(checks) != expected:
        print(f"  FAIL 判据条数 {len(checks)} != {expected}，有分支被跳过或漏加")
        return 1
    return 0 if passed == len(checks) else 1


# ── 入口 ──────────────────────────────────────────────────────────────────────

def load_arm(path: Path) -> list[dict]:
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as error:
            print(f"  ! {path}:{number} 不是合法 JSON，跳过：{error}")
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset")
    parser.add_argument("--jsonl", action="append", default=[], metavar="标签=路径",
                        help="一臂一个，例如 --jsonl R=out/R.jsonl --jsonl W=out/W.jsonl")
    parser.add_argument("--scope", default="cited", choices=["cited", "retrieved"],
                        help="attribution 口径：cited=正文标了编号的（默认）retrieved=登记过的全部")
    parser.add_argument("--detail", action="store_true", help="打印每题明细")
    parser.add_argument("--json", dest="json_out", help="把判定结果写成 JSON")
    parser.add_argument("--contrast-words", help="换一份 STRONG 档对照说明词表，逗号分隔（WEAK 档不变）")
    parser.add_argument("--self-test", action="store_true", help="只跑判据自检")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.dataset or not args.jsonl:
        parser.error("--dataset 与至少一个 --jsonl 是必需的（或者用 --self-test）")

    contrast = (tuple(word.strip() for word in args.contrast_words.split(",") if word.strip())
                if args.contrast_words else None)
    samples = {item["id"]: item
               for item in yaml.safe_load(Path(args.dataset).read_text(encoding="utf-8"))["samples"]}

    verdicts: dict[str, list[dict]] = {}
    summaries: dict[str, dict] = {}
    for spec in args.jsonl:
        if "=" not in spec:
            parser.error(f"--jsonl 要写成 标签=路径，收到 {spec!r}")
        label, path = spec.split("=", 1)
        records = load_arm(Path(path))
        unknown = sorted({item.get("sample_id") for item in records} - set(samples))
        if unknown:
            print(f"  ! {label}: {len(unknown)} 个 sample_id 不在题目集里，跳过：{unknown[:5]}")
        rows = [judge_record(item, samples[item["sample_id"]], contrast)
                for item in records if item.get("sample_id") in samples]
        verdicts[label] = rows
        summaries[label] = summarize(rows, args.scope)
        print_arm(label, summaries[label])
        if args.detail:
            print_detail(label, rows, args.scope)

    if len(summaries) >= 2:
        print_compare(summaries)

    if args.json_out:
        payload = {"scope": args.scope,
                   "summaries": {label: _jsonable(item) for label, item in summaries.items()},
                   "verdicts": verdicts}
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"\n判定结果 → {args.json_out}")
    return 0


def _jsonable(summary: dict) -> dict:
    """Counter 直接 dump 出来是 dict，但嵌在里面时要显式转，否则顺序不稳。"""
    out = {}
    for key, value in summary.items():
        if isinstance(value, dict):
            out[key] = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in value.items()}
        else:
            out[key] = value
    return out


if __name__ == "__main__":
    sys.exit(main())
