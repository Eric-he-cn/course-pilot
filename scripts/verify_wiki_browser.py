"""知识页全功能浏览器验证：对着 e2e_wiki 评测 W 臂建出的真实数据，16 条判据逐条跑。

覆盖：知识页树与折叠、lint 体检行、构建行文案、页内 [p.N] 与 [p.N-M] 引用按钮与抽屉、复合标注保持
纯文本、跨教材配对 chips 与往返跳转、手写区编辑/保存/刷新持久/清空、聊天侧知识页引用行、
知识页抽屉的教材出处按钮点开原文。

所有操作用真 Locator click/fill；evaluate 只用于读取与滚动，不用于点击——
evaluate 里的 click() 绕过命中测试，测不出元素被覆盖层挡住。

用法（起独立实例指向 W 臂数据目录，别对着开发库跑）：
    CP_PORT_OFFSET=2 STORAGE_DATA_DIR=<W 臂数据目录> ./scripts/dev.sh    # 8002 + 5175
    .venv/bin/python scripts/verify_wiki_browser.py \
        [--base http://127.0.0.1:5175] [--shots testdata/wiki-verify-shots]

判据钉在那份数据上：课程「深度学习综合」、知识页 197 页、抽样的页名与会话内容。
换一份数据要先改脚本里的这些常量，页数判据（A1 的 197）也要跟着标。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 与前端 CITE_MARK 完全一致（App.tsx）。区间形态 [p.12-14] / [文档 pp.12-14] 也认，
# 少认一种会让「按钮数不超过标注数」这条判据假红。
CITE_RE = re.compile(r"\[(?:[^\]\n]+ )?pp?\.\d+(?:-\d+)?\]|\[[^\]\n]+\.(?:pdf|docx?|pptx?|txt|md)\]", re.I)

results: list[tuple[str, bool, str]] = []


def check(cid: str, ok: bool, detail: str = "") -> None:
    results.append((cid, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} [{cid}] {detail}")


def exact(name: str):
    return re.compile("^" + re.escape(name) + "$")


def open_wiki_page(page, name: str) -> None:
    target = page.locator(".concept-tree .tree-open").filter(has_text=exact(name)).first
    target.click()
    page.wait_for_function(
        "name => { const b = document.querySelector('.note-viewer-head b'); return !!b && b.textContent === name }",
        arg=name, timeout=10000)
    page.wait_for_timeout(700)  # 出处 anchors 是第二个请求，等它落定


def cite_stats(page) -> tuple[int, int]:
    body = page.locator(".note-viewer .message-content").first
    text = body.inner_text()
    total = len(CITE_RE.findall(text))
    buttons = body.locator(".wiki-cite").count()
    return total, buttons


def goto_wiki_tab(page) -> None:
    page.locator(".main-nav button").filter(has_text="知识库").click()
    page.locator(".tabs button").filter(has_text="知识页").click()
    page.wait_for_selector(".concept-tree .concept-toggle", timeout=20000)
    page.wait_for_timeout(400)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:5175",
                        help="前端地址，默认 CP_PORT_OFFSET=2 那套实例的 5175")
    parser.add_argument("--shots", default=str(ROOT / "testdata" / "wiki-verify-shots"),
                        help="截图输出目录")
    args = parser.parse_args()
    base = args.base
    shots = pathlib.Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2)
        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(800)

        # ---- 登录 local ----
        if page.locator(".login-card").count():
            page.locator(".login-card input").first.fill("local")
            page.locator(".login-submit").click()
        page.wait_for_selector(".course-choice", timeout=15000)
        page.wait_for_timeout(600)

        # ---- 进课程 ----
        page.locator(".course-choice").filter(has_text="深度学习综合").first.click()
        page.wait_for_timeout(800)

        # ============ A. 知识页面板 ============
        goto_wiki_tab(page)

        # A1: 页数、树、折叠
        heading = page.locator(".card-heading h2").filter(has_text="知识页").first.inner_text()
        m = re.search(r"(\d+)", heading)
        n_heading = int(m.group(1)) if m else -1
        n_rows = page.locator(".concept-tree .concept-row").count()
        rows_info = page.eval_on_selector_all(
            ".concept-tree .concept-row",
            "els => els.map(e => ({ pad: e.style.paddingLeft, label: (e.querySelector('.tree-open, b') || {textContent: ''}).textContent }))")
        roots = [r["label"] for r in rows_info if r["pad"] in ("0px", "")]
        check("A1.count", n_heading == 197 and n_rows == 197,
              f"标题页数={n_heading}，树行数={n_rows}（判据 197）")
        print(f"  A1 根节点 {len(roots)} 个（按界面顺序）：{roots}")

        # 折叠/展开：点第一个展开态箭头
        first_toggle = page.locator(".concept-tree .concept-toggle.open").first
        toggled_label = first_toggle.get_attribute("aria-label")
        before = page.locator(".concept-tree .concept-row").count()
        first_toggle.click()
        page.wait_for_timeout(300)
        after_collapse = page.locator(".concept-tree .concept-row").count()
        collapsed_toggle = page.locator(".concept-tree .concept-toggle").filter(has_text="›").first
        # 找回同一个节点（aria-label 匹配）重新展开
        reopen = page.locator(f".concept-tree .concept-toggle[aria-label='{toggled_label}']").first
        aria_after = reopen.get_attribute("aria-expanded")
        reopen.click()
        page.wait_for_timeout(300)
        after_expand = page.locator(".concept-tree .concept-row").count()
        check("A1.collapse", after_collapse < before and after_expand == before and aria_after == "false",
              f"折叠「{toggled_label}」：{before}→{after_collapse} 行，aria-expanded={aria_after}，再展开回到 {after_expand}")

        # A2: 体检行
        lint_text = page.locator(".wiki-lint p").first.inner_text() if page.locator(".wiki-lint").count() else "(无 .wiki-lint)"
        check("A2.lint", "体检通过" in lint_text, f"体检行文案：「{lint_text.strip()}」")

        # A3: 构建行文案（覆盖率 or 预算预估），oversized/兜底提示不该出现
        rows = page.locator(".wiki-card .material-row")
        a3_texts = []
        for i in range(rows.count()):
            a3_texts.append(rows.nth(i).inner_text().replace("\n", " | "))
        joined = " || ".join(a3_texts)
        has_coverage = "个小节，已生成" in joined
        has_estimate = "预计 " in joined and "次模型调用" in joined
        bad_oversized = "超过大小上限" in joined
        bad_fallback = "目录改用概念表" in joined
        check("A3.coverage", (has_coverage or has_estimate) and not bad_oversized and not bad_fallback,
              f"覆盖率文案={has_coverage}，预算预估={has_estimate}，oversized 提示={bad_oversized}，兜底提示={bad_fallback}")
        for t in a3_texts:
            print(f"  A3 构建行：{t}")

        page.screenshot(path=str(shots / "01-wiki-tree-lint.png"))

        # ============ B. 页内引用 ============
        survey = [
            "Dropout 正则化的原理与操作",           # dl-notes，全命名形态
            "正则化：防过拟合的原理与实现",         # dl-notes，含裸 [p.N]
            "梯度下降法实践2-学习率",               # ml-notes
            "正则化线性回归",                       # ml-notes，含复合标注 [doc p.32, p.33]
            "位置编码",                             # 同名两页，取第一个
            "GPT",                                  # happy-llm，预期无配对边
            "mini-batch梯度下降与计算机视觉入门",   # dl-notes，标注最多
            "循环神经网络模型",                     # d2l（精确匹配只有它）
        ]
        stats: list[tuple[str, int, int]] = []
        for name in survey:
            open_wiki_page(page, name)
            total, buttons = cite_stats(page)
            stats.append((name, total, buttons))
            print(f"  B4 「{name}」：正文标注 {total} 处，渲染成按钮 {buttons} 个，纯文本 {total - buttons} 处")
        sum_total = sum(s[1] for s in stats)
        sum_btn = sum(s[2] for s in stats)
        check("B4.ratio", sum_btn > 0 and sum_total >= sum_btn,
              f"抽样 {len(survey)} 页：标注共 {sum_total} 处，按钮 {sum_btn} 个（{sum_btn / max(sum_total, 1):.1%}），纯文本 {sum_total - sum_btn} 处")

        # B5: 点一个按钮 → 抽屉 + 原文
        open_wiki_page(page, "Dropout 正则化的原理与操作")
        page.wait_for_selector(".note-viewer .message-content .wiki-cite", timeout=8000)
        btn = page.locator(".note-viewer .message-content .wiki-cite").first
        btn_label = btn.inner_text()
        mm = re.match(r"^\[(?:(.+) )?p\.(\d+)\]$", btn_label)
        want_doc = (mm.group(1) or "") if mm else ""
        want_page = mm.group(2) if mm else "?"
        btn.click()
        page.wait_for_selector(".citation-drawer", timeout=8000)
        d_kind = page.locator(".citation-drawer header p").first.inner_text()
        d_head = page.locator(".citation-drawer header h2").first.inner_text()
        d_loc = page.locator(".citation-drawer .citation-location").first.inner_text()
        d_quote = page.locator(".citation-drawer blockquote").first.inner_text()
        check("B5.drawer",
              d_kind == "教材引用" and d_head == want_doc and f"第 {want_page} 页" == d_loc and len(d_quote.strip()) > 30,
              f"点「{btn_label}」→ 抽屉[{d_kind} / {d_head} / {d_loc}]，blockquote {len(d_quote)} 字")
        page.screenshot(path=str(shots / "04-cite-drawer-material.png"))
        page.locator(".citation-drawer header button").click()
        page.wait_for_timeout(300)

        # B6: 对不上的标注保持纯文本（复合形态 [doc p.32, p.33]）
        open_wiki_page(page, "正则化线性回归")
        body = page.locator(".note-viewer .message-content").first
        body_text = body.inner_text()
        composite_in_text = body_text.count("[ml-notes-slice.pdf p.32, p.33]")
        composite_btn = body.locator(".wiki-cite").filter(has_text="p.32, p.33").count()
        single_btn = body.locator(".wiki-cite").count()
        check("B6.plain", composite_in_text >= 1 and composite_btn == 0 and single_btn >= 1,
              f"复合标注 [ml-notes-slice.pdf p.32, p.33] 在正文出现 {composite_in_text} 次、成按钮 {composite_btn} 次；"
              f"同页单页码按钮 {single_btn} 个")

        # ============ C. 跨教材配对 ============
        # C8a: 有边的页显示 chips，带教材名小字，点击跳转、可返回
        open_wiki_page(page, "Dropout 正则化的原理与操作")
        pairs = page.locator(".note-viewer .wiki-pairs")
        has_pairs = pairs.count() > 0
        chips_info = []
        if has_pairs:
            chips = pairs.locator(".pair-chip")
            for i in range(chips.count()):
                c = chips.nth(i)
                doc = c.locator("i").inner_text() if c.locator("i").count() else ""
                chips_info.append((c.inner_text().replace(doc, "").strip(), doc))
        label_ok = has_pairs and "其他来源也讲了这个" in pairs.inner_text()
        print(f"  C8 「Dropout 正则化的原理与操作」chips：{chips_info}")
        target_chip = pairs.locator(".pair-chip").filter(has_text="暂退法").first if has_pairs else None
        check("C8.chips", label_ok and target_chip is not None and target_chip.count() > 0,
              f"配对区标签={label_ok}，含「暂退法（Dropout）」chip={target_chip.count() > 0 if target_chip else False}，"
              f"chips 共 {len(chips_info)} 个（均带教材名小字：{all(d for _, d in chips_info)}）")
        # 滚动到 chips 截图（含正文按钮）
        pairs.first.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / "02-wiki-page-cites-pairs.png"))

        # 点 chip 跳到那页
        target_chip.click()
        page.wait_for_function(
            "() => { const b = document.querySelector('.note-viewer-head b'); return !!b && b.textContent.includes('暂退法') }",
            timeout=10000)
        page.wait_for_timeout(700)
        landed = page.locator(".note-viewer-head b").inner_text()
        # 在对面页上找回来的 chip（边是对称的）
        back_chip = page.locator(".note-viewer .wiki-pairs .pair-chip").filter(has_text="Dropout 正则化的原理与操作").first
        back_ok = back_chip.count() > 0
        if back_ok:
            back_chip.click()
            page.wait_for_function(
                "() => { const b = document.querySelector('.note-viewer-head b'); return !!b && b.textContent === 'Dropout 正则化的原理与操作' }",
                timeout=10000)
            page.wait_for_timeout(500)
        returned = page.locator(".note-viewer-head b").inner_text()
        check("C8.jump", landed.startswith("暂退法") and back_ok and returned == "Dropout 正则化的原理与操作",
              f"跳到「{landed}」，对面有回程 chip={back_ok}，点回后标题=「{returned}」")

        # C8b: 无边的页不渲染 chips 区块
        open_wiki_page(page, "GPT")
        gpt_pairs = page.locator(".note-viewer .wiki-pairs").count()
        check("C8.noedge", gpt_pairs == 0, f"「GPT」页 .wiki-pairs 区块数={gpt_pairs}（应为 0，无空壳）")

        # ============ D. 手写区 ============
        open_wiki_page(page, "Dropout 正则化的原理与操作")
        hand_entry = page.locator(".note-viewer .wiki-hand").first
        hand_entry.locator(".text-button").filter(has_text="添加补充").click()
        editor = page.locator(".wiki-hand-editor")
        editor.wait_for(timeout=5000)
        editor.fill("这是**加粗**测试，附一个对不上的页码 [p.99]，还有一行\n\n- 列表项 markdown")
        page.screenshot(path=str(shots / "05-handwritten-editing.png"))
        page.locator(".wiki-hand-actions .ghost-button").filter(has_text=exact("保存")).click()
        page.wait_for_selector(".wiki-hand .message-content", timeout=8000)
        hand = page.locator(".wiki-hand").first
        hand_title = hand.locator("h3").inner_text()
        strong_ok = hand.locator(".message-content strong").filter(has_text="加粗").count() == 1
        hand_text = hand.locator(".message-content").inner_text()
        p99_plain = "[p.99]" in hand_text
        p99_btn = hand.locator(".wiki-cite").count()
        check("D9.save", hand_title == "我的补充" and strong_ok and p99_plain and p99_btn == 0,
              f"标题=「{hand_title}」，**加粗** 渲染成 strong={strong_ok}，[p.99] 纯文本={p99_plain}，手写区按钮数={p99_btn}")
        hand.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / "06-handwritten-saved.png"))

        # D11（保存态）：marker 注释不上屏
        marker_now = "以下是手写区" in page.content()
        # D10: 刷新 → 重新打开 → 手写区仍在
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        if page.locator(".login-card").count():
            page.locator(".login-card input").first.fill("local")
            page.locator(".login-submit").click()
            page.wait_for_timeout(800)
        page.wait_for_selector(".course-choice", timeout=15000)
        page.locator(".course-choice").filter(has_text="深度学习综合").first.click()
        page.wait_for_timeout(600)
        goto_wiki_tab(page)
        open_wiki_page(page, "Dropout 正则化的原理与操作")
        persisted = page.locator(".wiki-hand .message-content")
        persist_ok = persisted.count() > 0 and "加粗" in persisted.first.inner_text() and "[p.99]" in persisted.first.inner_text()
        marker_after_reload = "以下是手写区" in page.content()
        # 再编辑 → 清空 → 保存 → 回到入口
        page.locator(".wiki-hand-head .text-button").filter(has_text=exact("编辑")).click()
        editor = page.locator(".wiki-hand-editor")
        editor.wait_for(timeout=5000)
        editor.fill("")
        page.locator(".wiki-hand-actions .ghost-button").filter(has_text=exact("保存")).click()
        page.wait_for_selector(".wiki-hand.empty", timeout=8000)
        entry_back = page.locator(".wiki-hand.empty .text-button").filter(has_text="添加补充").count()
        check("D10.persist", persist_ok and entry_back == 1,
              f"刷新后手写区仍在={persist_ok}；清空保存后回到入口={entry_back == 1}")
        check("D11.marker", not marker_now and not marker_after_reload,
              f"marker「以下是手写区…」出现在 DOM：保存态={marker_now}，刷新后={marker_after_reload}")

        # ============ E. 聊天侧 ============
        page.locator(".main-nav button").filter(has_text=exact("对话")).click()
        page.wait_for_timeout(800)
        session = page.locator(".session").filter(has_text="dropout 在训练和测试阶段").first
        session.click()
        page.wait_for_selector(".citations", timeout=15000)
        page.wait_for_timeout(800)
        cites = page.locator(".citations").last
        wiki_rows = cites.locator(".cite-row.wiki")
        mat_rows = cites.locator(".cite-row.mat")
        n_wiki, n_mat = wiki_rows.count(), mat_rows.count()
        wiki_who = wiki_rows.first.locator(".cite-who").inner_text() if n_wiki else ""
        wiki_at = wiki_rows.first.locator(".cite-at").inner_text() if n_wiki else ""
        mat_at = mat_rows.first.locator(".cite-at").inner_text() if n_mat else ""
        check("E12.rows",
              n_wiki >= 1 and n_mat >= 1 and wiki_who.startswith("知识页 · ") and wiki_at == "转述" and bool(re.match(r"^p\.\d+$", mat_at)),
              f"wiki 行 {n_wiki} 条（who=「{wiki_who}」at=「{wiki_at}」），教材行 {n_mat} 条（at=「{mat_at}」）")
        cites.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / "07-chat-wiki-rows.png"))

        # E13: 点 wiki 引用 → 转述 + 教材出处按钮（按教材分组、可点开原文）
        wiki_rows.first.click()
        page.wait_for_selector(".citation-drawer", timeout=8000)
        d_kind = page.locator(".citation-drawer header p").first.inner_text()
        d_head = page.locator(".citation-drawer header h2").first.inner_text()
        d_loc = page.locator(".citation-drawer .citation-location").first.inner_text()
        d_quote = page.locator(".citation-drawer blockquote").first.inner_text()
        srcs = page.locator(".citation-drawer .citation-sources")
        has_sources = srcs.count() > 0
        span_docs, page_btns = [], 0
        if has_sources:
            spans = srcs.locator(".citation-span")
            for i in range(spans.count()):
                span_docs.append(spans.nth(i).locator("b").inner_text())
            page_btns = srcs.locator(".citation-span button").count()
        check("E13.wiki_drawer",
              d_kind == "知识页引用" and "转述稿" in d_loc and len(d_quote.strip()) > 30 and has_sources and page_btns >= 1,
              f"抽屉[{d_kind} / {d_head}]，location=「{d_loc}」，blockquote {len(d_quote)} 字；"
              f"出处分组 {span_docs}，页码按钮 {page_btns} 个")
        page.screenshot(path=str(shots / "08-wiki-citation-drawer.png"))
        # 点第一颗页码按钮 → 换成教材原文抽屉
        if has_sources and page_btns:
            first_btn = srcs.locator(".citation-span button").first
            btn_text = first_btn.inner_text()
            first_btn.click()
            page.wait_for_timeout(600)
            d2_kind = page.locator(".citation-drawer header p").first.inner_text()
            d2_head = page.locator(".citation-drawer header h2").first.inner_text()
            d2_quote = page.locator(".citation-drawer blockquote").first.inner_text()
            check("E13.source_open",
                  d2_kind == "教材引用" and len(d2_quote.strip()) > 30,
                  f"点「{btn_text}」→ 抽屉[{d2_kind} / {d2_head}]，blockquote {len(d2_quote)} 字")
            page.screenshot(path=str(shots / "09-wiki-source-opened.png"))

        browser.close()

    print("\n===== 汇总 =====")
    passed = sum(1 for _, ok, _ in results if ok)
    for cid, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {cid}")
    print(f"{passed}/{len(results)} 判据通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
