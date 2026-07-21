# CoursePilot 2.0 视觉方案：Terminal

本文是前端的视觉识别方案，落地于 `frontend/src/styles.css`。布局、信息架构与响应式规则仍见 [前端设计](coursepilot-2.0-frontend-design.md)，本文覆盖其中的视觉方向部分。设计稿见 [design-draft-b-terminal.html](design-draft-b-terminal.html)。

## 1. 概念

**开发者工具的浅色模式**（Linear / Vercel / Raycast 一系）：纯白画布、锐利细边框、等宽字体承担元数据、把「系统正在做什么」当作设计语言的一部分。这与产品强调证据、引用、trace、可审计的理念一致——界面本身就应该透明、精确、可信。

国际化、极简、极客；明确反对：AI 通用风（渐变、AI 紫、大圆角胶囊、弥散阴影）、中文字 logo、彩色装饰。

## 2. 识别元素

1. **`>_` 方块 logo**：黑底白字等宽 `>_` + CoursePilot 字标 + `v2.0` 版本 tag。
2. **`❯` 提示符**：Agent 回答头部（`❯ CoursePilot`）与输入框前缀，唯一的「角色」符号。
3. **等宽元数据层**：分区标签（WORKSPACE / NAV / SESSIONS）、导航编号（01–04）、时间、页码、分数、状态一律等宽字体；正文与控件保持无衬线。
4. **代码式引用**：教材引用渲染为 `[1] d2l-en.pdf:409`（file:line 格式），收在带 `SOURCES · n` 标签的引用面板里。
5. **底部状态栏**：常驻一条 26px 等宽状态栏，展示真实的连接状态、provider/model、检索 backend——不是装饰，读的是 health 接口。
6. **戳记状态**：状态标签为等宽小字 + 1px 描边，无填充底色。

## 3. 设计 token

```css
--bg:     #FFFFFF   /* 画布：纯白 */
--panel:  #FAFAFA   /* 面板：侧栏、气泡、代码块 */
--text:   #18181B
--muted:  #71717A
--border: #E4E4E7   /* 1px 锐利边框，分区只靠它 */
--accent: #059669   /* 终端绿：只用于 icon、提示符、激活态、状态点 */
--danger: #DC2626
--warning:#B45309
```

- 圆角 6–8px；表意性圆形控件（开关、色点）除外。
- 阴影只允许 `0 1px 2px rgba(0,0,0,.04)` 级别的贴地投影。
- 主题色不做大面积填充；仅索引流水线「进行中」步骤允许 accent 实底。
- 课程色由服务端下发，仅用于小色点。

## 4. 字体

```css
--sans: -apple-system,BlinkMacSystemFont,"Helvetica Neue","PingFang SC","Noto Sans SC",sans-serif
--mono: ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace
```

| 用途 | 字族 | 规格 |
| --- | --- | --- |
| 页面标题 / hero | sans | 24px，字重 650，字距 -0.02em |
| 正文（含 Agent 回答） | sans | 14–14.5px / 行高 1.75 |
| 分区标签、编号、引用、状态栏、时间与分数 | mono | 10–12px，标签字距 .1em |
| 卡片标题 | sans | 15px，字重 600 |

只用系统字体栈，不引入 webfont。

## 5. 组件规范

- **按钮**：主按钮墨底白字 6px 圆角；次级按钮白底 1px 边框；破坏性操作红字。
- **输入区（composer）**：1px 边框 + 贴地投影，左侧 `❯` 绿色提示符；聚焦时边框变 accent。
- **选中态**：白底 + 1px 边框 + 贴地投影（浮出面板），不用填充色表达选中。
- **引用面板**：回答末尾的浅灰面板，`SOURCES · n` mono 标签 + 代码式引用链；点击打开右侧引用抽屉。
- **索引流水线**：mono 戳记步骤，已完成=墨字，进行中=accent 实底白字，未开始=灰字；进度条 4px accent。
- **空态**：`❯` 符号 + 标题 + 一句说明，不用插画。

## 6. 动效

- 只保留必要过渡：侧栏收合、开关滑块、边框颜色，一律 ≤ 180ms。
- 不做入场动画、骨架屏闪烁和渐变流光。`prefers-reduced-motion` 下全部关闭。

## 7. 禁止清单

- 渐变、玻璃拟态、弥散阴影、大圆角胶囊。
- 中文字或表意字符作为 logo / 图标。
- 彩色填充的状态标签与 chip。
- 装饰性 emoji / 插画作为界面元素。
- 状态栏展示假数据；所有系统信息必须来自真实接口。
