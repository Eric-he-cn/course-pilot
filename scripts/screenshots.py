"""生成文档用截图。UI 改了重跑一次即可，截图不会过期。

每张图要的画面都由端到端旅程建出来（引用、工具链、图示、计划、笔记、错题、知识页），
所以顺序是：起一套独立实例 → 跑旅程建数据 → 截图。别对着开发库跑，context 那张会真发一轮。

    CP_PORT_OFFSET=1 STORAGE_DATA_DIR=testdata/shots ./scripts/dev.sh
    .venv/bin/python scripts/e2e_journey.py --base http://127.0.0.1:8001 --data-dir testdata/shots
    .venv/bin/python scripts/screenshots.py --url http://127.0.0.1:5174

数据目录得是新布局（<data>/users/<user_id>/）。旧布局先跑一次
scripts/migrate_to_users.py --data-dir <那个目录>。
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
        "name": "login.png",
        "caption": "登录：输入用户名或从随机建议里挑一个；每个用户名对应一份独立的工作区",
        "script": "localStorage.removeItem('cp-username'); location.reload();",
        "wait": 1500,
        "expect": ".login-card",
    },
    {
        "name": "chat-citation.png",
        "caption": "对话取证：三类来源在依据面板里一行一条，左侧色条区分教材/知识页/网络，出处右对齐成列",
        "clicks": ["操作系统", "对话"],
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>/分成哪几部分/.test(e.textContent)); s&&s.click();",
        "settle": "const c=document.querySelector('.citations'); const m=document.querySelector('.messages'); if(c&&m) m.scrollTop=c.offsetTop+c.offsetHeight-m.clientHeight+120;",
        # 判据要能验到「三类引用视觉可分」这件事，光有 .citations 会把只剩教材引用的画面也放过去
        "expect": ".cite-row.wiki",
    },
    {
        "name": "chat-tools.png",
        "caption": "工具链可见：自动加载 research、检索教材、联网检索、抓取网页，失败的那一步也显示出来",
        "clicks": ["操作系统", "对话"],
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>e.textContent.includes('冲刺'))||document.querySelector('.session'); s&&s.click();",
        "settle": (
            "const c=[...document.querySelectorAll('.tool-chip')].find(e=>/联网检索|Web search/.test(e.textContent));"
            "const m=document.querySelector('.messages'); const msg=c&&c.closest('.message');"
            "if(msg&&m) m.scrollTop=msg.offsetTop-40;"
        ),
        # 只等 .tool-chip 的话，找不到联网那一轮时会拍出和 chat-citation 一模一样的画面
        "expect": ".tool-chip:has-text('联网检索')",
    },
    {
        "name": "chat-diagram.png",
        "caption": "图示：mermaid 渲染成 SVG，下方可下载",
        "clicks": ["操作系统", "对话"],
        # 侧栏那一行显示的是会话标题，按轮次里的话去找永远找不到——图示那一轮在主会话里
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>e.textContent.includes('冲刺'))||document.querySelector('.session'); s&&s.click();",
        # mermaid 是动态 import 的（600KB+），首次渲染比想象的慢，所以滚动要等它出来之后
        "wait": 8000,
        "after": "const f=document.querySelector('.mermaid-figure'); const m=document.querySelector('.messages'); if(f&&m) m.scrollTop=f.offsetTop-80;",
        "expect": ".mermaid-figure svg",
    },
    {
        "name": "library-wiki.png",
        "caption": "知识页：按教材目录自底向上编出来的页面，成树展示，可直接读；未建时先给预算预估",
        # 挑有书签层级的那门课：无目录的教材走顺序切段，知识页会平铺成一列，看不出树
        "clicks": ["深度学习", "知识仓库"],
        "script": "const t=[...document.querySelectorAll('.tabs button')].find(e=>e.textContent.includes('知识页')); t&&t.click();",
        "wait": 1500,
        # 要的是「层级画出来了」，所以判据落在可折叠节点上，光等 .tree-open 连平铺也算过
        "expect": ".concept-tree .concept-toggle",
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
        "caption": "学习计划：周网格看这周排满了没、今天要做什么，下面按天列出完整条目；改动通过对话发生，历史条目不被改写",
        "clicks": ["操作系统", "学习计划"],
        "expect": ".plan-weeks .day.today .task",
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
        "name": "model-picker.png",
        "caption": "底部状态栏：模型、思考、思考深度、界面语言四个下拉；配几个模型由 .env 决定",
        "clicks": ["操作系统", "对话"],
        "expect": ".statusbar-picker select",
        "clip": ".statusbar",
    },
    {
        "name": "delete-confirm.png",
        "caption": "删除前列出连带影响：删一门课会带走它的教材、概念、掌握度、计划与会话",
        "clicks": ["管理与设置"],
        "settle": "const b=[...document.querySelectorAll('.settings-course button')].find(e=>e.innerText.trim()==='删除'); b&&b.click();",
        "wait": 700,
        "expect": ".danger-confirm",
    },
    {
        "name": "context.png",
        "caption": "上下文构成：输入框旁的占比环，点开是堆叠条与按占比降序的分区，小分区折成一行",
        "clicks": ["操作系统", "对话"],
        # 上下文条只在流式期间有数据（回读历史消息时不带这些字段），所以这张要真发一轮
        "script": (
            "const s=[...document.querySelectorAll('.session')].find(e=>e.textContent.includes('冲刺'))"
            "||document.querySelector('.session'); s&&s.click();"
        ),
        "settle": (
            "const ta=document.querySelector('textarea');"
            "Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(ta,'RR 的时间片该怎么选？');"
            "ta.dispatchEvent(new Event('input',{bubbles:true})); ta.closest('form').requestSubmit();"
        ),
        "after": (
            # 弹层往上展开，正文压太矮它就顶出屏幕；分段多了一倍之后要留够这个高度
            "const m=document.querySelector('.messages'); if(m) m.style.maxHeight='330px';"
            "const b=document.querySelector('.context-chip > button'); b&&b.click();"
        ),
        # 这张必须真发一轮才有数据，而一轮现在要 30 秒以上
        "wait": 50000,
        "expect": ".context-popover",
    },
    {
        "name": "dev-trace.png",
        "caption": "开发者模式：点 Agent 回复开头的名字调出这一轮的 trace，先列 ReAct 每一轮，统计折在后面",
        # 开关存在浏览器本地，几张图共用一个页面，所以这张放最后，免得开发者模式漏给别的图
        "local_storage": {"cp-devmode": "on"},
        "clicks": ["操作系统", "对话"],
        "script": "const s=[...document.querySelectorAll('.session')].find(e=>e.textContent.includes('冲刺'))||document.querySelector('.session'); s&&s.click();",
        # 挑联网那一轮：它的 ReAct 有多个 round、多个工具，才看得出这个面板在讲什么
        "settle": (
            "const c=[...document.querySelectorAll('.tool-chip')].find(e=>/联网检索|Web search/.test(e.textContent));"
            "const msg=c&&c.closest('.message'); const m=document.querySelector('.messages');"
            "if(msg&&m) m.scrollTop=msg.offsetTop-40;"
            "const b=msg&&msg.querySelector('.agent-name'); b&&b.click();"
        ),
        "after": "const y=document.querySelector('.trace-confirm-yes'); y&&y.click();",
        # 等到 ReAct 那一段真渲染出来，只等 .trace-drawer 会把「侧栏开了但读不到 trace」算过
        "expect": ".trace-drawer .trace-step",
        "final": (
            "const d=document.querySelector('.trace-drawer');"
            "const f=d&&d.querySelector('.trace-turn.focused');"
            "if(d&&f) d.scrollTop=f.offsetTop-70;"
        ),
    },
]


LOGIN_SNIPPET = """
user => {
  if (!document.querySelector('.login-card')) return
  const input = document.querySelector('.login-card input')
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, user)
  input.dispatchEvent(new Event('input', { bubbles: true }))
  document.querySelector('.login-submit').click()
}
"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright：.venv/bin/python -m pip install playwright && "
              ".venv/bin/python -m playwright install chromium")
        return 2

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5174")
    parser.add_argument("--user", default="local", help="用哪个用户名登录，要和实例里的数据对应")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--only", default="", help="只跑这些图，逗号分隔（不带 .png 也行）")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    written: list[tuple[str, str]] = []
    failures: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height}, device_scale_factor=2)
        wanted = {name.removesuffix('.png') for name in args.only.split(',') if name.strip()}
        for shot in SHOTS:
            if wanted and shot["name"].removesuffix('.png') not in wanted:
                continue
            page.goto(args.url, wait_until="networkidle")
            page.wait_for_timeout(900)
            if shot.get("local_storage"):
                # 界面开关存在 localStorage，写完必须刷新：读它的是 useState 初值。
                page.evaluate(
                    "entries => { for (const [key, value] of Object.entries(entries)) localStorage.setItem(key, value) }",
                    shot["local_storage"],
                )
                page.reload(wait_until="networkidle")
                page.wait_for_timeout(900)
            # 登录页会拦住后面每一个画面（login 那张要的就是登录页本身）。不在这里进去，
            # 后续 shot 全对着空页面跑，脚本会以一个看不出原因的 Illegal invocation 收场。
            if shot["name"] != "login.png":
                page.evaluate(LOGIN_SNIPPET, args.user)
                page.wait_for_timeout(1600)
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
            if expect:
                try:
                    # 用 wait_for 而不是当场数一次：渲染慢一点不该算失败，真不出现才算。
                    page.locator(expect).first.wait_for(timeout=8000)
                except Exception:
                    # 宁可失败也不产出一张内容不对的图：配错图比没图更糟。
                    failures.append(f"{shot['name']}：等待的元素 {expect} 没出现，画面可能不对")
                    continue
            if shot.get("final"):
                # 内容出来之后才能滚到它。放在 expect 之后，免得对着还没渲染的东西算位置。
                page.evaluate(f"() => {{ {shot['final']} }}")
                page.wait_for_timeout(500)
            target = OUT / shot["name"]
            if shot.get("clip"):
                # 窄条（状态栏之类）整页截出来看不清，按元素裁一张
                element = page.locator(shot["clip"]).first
                element.screenshot(path=str(target))
            else:
                page.screenshot(path=str(target))
            written.append((shot["name"], shot["caption"]))
            print(f"  {shot['name']}  {shot['caption']}")
        browser.close()

    # 索引按 SHOTS 定义写全，不按本次实际产出——否则 --only 跑一张就把索引清成一行。
    index = OUT / "README.md"
    index.write_text(
        "# 文档截图\n\n由 `scripts/screenshots.py` 生成，UI 改动后重跑一次即可。\n\n"
        + "\n".join(f"- `{shot['name']}` — {shot['caption']}" for shot in SHOTS) + "\n",
        encoding="utf-8",
    )
    print(f"\n共 {len(written)} 张，输出在 {OUT.relative_to(ROOT)}")
    for problem in failures:
        print(f"  跳过 {problem}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
