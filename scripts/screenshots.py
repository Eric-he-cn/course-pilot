"""生成文档用截图。UI 改了重跑一次即可，截图不会过期。

需要一个已有数据的实例在跑（推荐端到端旅程刚跑完的那个）：
    STORAGE_DATA_DIR=testdata/e2e-fresh .venv/bin/python -m uvicorn app.main:app \
        --app-dir backend --host 127.0.0.1 --port 8001
    cd frontend && VITE_PROXY_TARGET=http://127.0.0.1:8001 pnpm dev --port 5174
    .venv/bin/python scripts/screenshots.py --url http://127.0.0.1:5174
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Docs" / "images"

# 每张图：文件名、说明、到达该画面要点的按钮文字序列、可选的收尾脚本
SHOTS: list[dict] = [
    {
        "name": "chat-citation.png",
        "caption": "对话取证：回答带教材文件名与页码，公式正常渲染，底部 SOURCES 可点开原文",
        "clicks": ["操作系统", "对话"],
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>e.textContent.includes('冲刺'))||document.querySelector('.session'); s&&s.click();",
        "settle": "const m=document.querySelector('.messages'); if(m) m.scrollTop=0;",
        "expect": ".citations",
    },
    {
        "name": "chat-tools.png",
        "caption": "工具链可见：自动加载 research、检索教材、联网检索、抓取网页，失败的那一步也显示出来",
        "clicks": ["操作系统", "对话"],
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>/EEVDF|联网/.test(e.textContent)); s&&s.click();",
        "settle": "const m=document.querySelector('.messages'); if(m) m.scrollTop=0;",
        "expect": ".tool-chip",
    },
    {
        "name": "chat-diagram.png",
        "caption": "图示：mermaid 渲染成 SVG，下方可下载",
        "clicks": ["操作系统", "对话"],
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>/流程图|STCF/.test(e.textContent)); s&&s.click();",
        "settle": "const f=document.querySelector('.mermaid-figure'); const m=document.querySelector('.messages'); if(f&&m) m.scrollTop=f.offsetTop-80;",
        "wait": 2500,
        "expect": ".mermaid-figure svg",
    },
    {
        "name": "library-notes.png",
        "caption": "课程笔记：助手整理并存下的学习卡片，可在界面直接查看",
        "clicks": ["操作系统", "知识仓库"],
        "script": "const t=[...document.querySelectorAll('.tabs button')].find(e=>e.textContent.includes('课程笔记')); t&&t.click();",
        "settle": "const b=[...document.querySelectorAll('.ghost-button')].find(e=>e.textContent==='查看'); b&&b.click();",
        "wait": 1200,
        "expect": ".note-viewer",
    },
    {
        "name": "plan.png",
        "caption": "学习计划：版本化的每日条目，挂着概念名，历史条目不会被改写",
        "clicks": ["操作系统", "学习计划"],
        "expect": ".material-row",
    },
    {
        "name": "archive.png",
        "caption": "学习档案：按概念的掌握度与证据事件；证据不足时显示「数据不足」而不是编一个百分比",
        "clicks": ["操作系统", "学习档案"],
        "expect": ".mastery-row, .material-row, .card",
    },
    {
        "name": "help.png",
        "caption": "使用说明：上手清单按真实状态打勾，能力卡与实例状态都读自接口",
        "clicks": ["使用说明"],
        "expect": ".help-step",
    },
    {
        "name": "context.png",
        "caption": "上下文构成：输入框旁的占比条，展开后逐段列出字符数",
        "clicks": ["操作系统", "对话"],
        # 上下文条只在流式期间有数据（回读历史消息时不带这些字段），所以这张要真发一轮
        "script": (
            "const s=[...document.querySelectorAll('.session')].find(e=>/调度器|联网/.test(e.textContent)); s&&s.click();"
        ),
        "settle": (
            "const ta=document.querySelector('textarea');"
            "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(ta,'RR 的时间片该怎么选？');"
            "ta.dispatchEvent(new Event('input',{bubbles:true})); ta.closest('form').requestSubmit();"
        ),
        "after": (
            "const m=document.querySelector('.messages'); if(m) m.style.maxHeight='150px';"
            "const b=document.querySelector('.context-chip > button'); b&&b.click();"
        ),
        "wait": 14000,
        "expect": ".context-popover",
    },
]


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：.venv/bin/python -m pip install playwright && "
              ".venv/bin/python -m playwright install chromium")
        return 2

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5174")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, str]] = []
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=2)
        for shot in SHOTS:
            page.goto(args.url, wait_until="networkidle")
            page.wait_for_timeout(900)
            for label in shot.get("clicks", []):
                page.evaluate(
                    "label => { const b=[...document.querySelectorAll('button')].find(e => e.textContent.includes(label)); b && b.click() }",
                    label,
                )
                page.wait_for_timeout(600)
            for key in ("script", "settle"):
                if shot.get(key):
                    page.evaluate(f"() => {{ {shot[key]} }}")
                    page.wait_for_timeout(700)
            page.wait_for_timeout(shot.get("wait", 400))
            if shot.get("after"):
                page.evaluate(f"() => {{ {shot['after']} }}")
                page.wait_for_timeout(800)
            expect = shot.get("expect")
            if expect and page.locator(expect).count() == 0:
                # 宁可失败也不产出一张内容不对的图：配错图比没图更糟。
                failures.append(f"{shot['name']}：等待的元素 {expect} 没出现，画面可能不对")
                continue
            target = OUT / shot["name"]
            page.screenshot(path=str(target))
            written.append((shot["name"], shot["caption"]))
            print(f"  {shot['name']}  {shot['caption']}")
        browser.close()

    index = OUT / "README.md"
    index.write_text(
        "# 文档截图\n\n由 `scripts/screenshots.py` 生成，UI 改动后重跑一次即可。\n\n"
        + "\n".join(f"- `{name}` — {caption}" for name, caption in written) + "\n",
        encoding="utf-8",
    )
    print(f"\n共 {len(written)} 张，输出在 {OUT.relative_to(ROOT)}")
    for problem in failures:
        print(f"  跳过 {problem}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
