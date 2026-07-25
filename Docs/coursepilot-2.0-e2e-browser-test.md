# CoursePilot 2.0 浏览器端到端测试

一次浏览过程覆盖《Demo 交付计划》第 5 节的全部验收项，外加工具循环、跨语言检索和图片提问。

教材是真实开源教材的章节切片，断言锚点都是教材里真实存在的事实与页码；回答里出现这些事实，
才说明证据真的被检索到，而不是模型凭通用知识说出来的。

## 1. 前置

### 1.1 驱动方式

用内置 Browser pane（`mcp__Claude_Browser__*`）驱动。它没有文件选择器，教材上传靠页面自己
`fetch` fixture 再注入到隐藏 input——`vite.config.ts` 的 `server.fs.allow` 只放开了
`data/e2e-fixtures` 这一个目录，仓库其余文件（含 `.env`）仍返回 403。

两个实测坑：`javascript_tool` 单次调用 30 秒超时；标签页不在前台时 `setTimeout` 被浏览器节流到
1 秒起。所以不要写长轮询，一次调用里的等待控制在 25 秒内，等不到就再发一次读取调用。

### 1.2 准备与启动

```bash
.venv/bin/python scripts/e2e_fixture.py
```

首次执行下载约 68 MB 开源教材，缓存在 `data/e2e-fixtures/source/`，重跑不再下载；随后切出章节、
光栅化一页 PNG，并清空 `data/e2e`。测试用独立数据目录，不碰开发库 `data/coursepilot.db`。

启动被测实例：`preview_start` 选 `coursepilot-e2e`，或

```bash
STORAGE_DATA_DIR=data/e2e ./scripts/dev.sh
```

`.env` 里 `COURSEPILOT_ENABLE_REMOTE_LLM=1` 必须开启，否则第 6 步起测的是本地 responder。
BGE 向量模型需已在本地缓存，否则首次索引会卡在下载模型。后端比前端晚几秒就绪，首屏可能闪一次
「请求失败（500）」，刷新即可。

### 1.3 页面内 helper

进页面后先装三个 helper，后续步骤都用它们（React 受控组件必须走原生 setter 才能触发 onChange）：

```js
window.__e2eUpload = async (selector, name) => {
  const base = '/@fs' + '<项目绝对路径>/data/e2e-fixtures/'
  const blob = await (await fetch(base + encodeURIComponent(name))).blob()
  const input = document.querySelector(selector)
  const dt = new DataTransfer(); dt.items.add(new File([blob], name, { type: blob.type }))
  input.files = dt.files
  input.dispatchEvent(new Event('change', { bubbles: true }))
}
window.__ask = async (course, text) => {
  [...document.querySelectorAll('.course-choice')].find(b => b.textContent.includes(course)).click()
  await new Promise(r => setTimeout(r, 400))
  ;[...document.querySelectorAll('.main-nav button')].find(b => b.textContent.includes('对话')).click()
  await new Promise(r => setTimeout(r, 600))
  const ta = document.querySelector('.composer textarea')
  Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, text)
  ta.dispatchEvent(new Event('input', { bubbles: true }))
  await new Promise(r => setTimeout(r, 200))
  document.querySelector('.send-button').click()
}
window.__read = () => {
  const last = [...document.querySelectorAll('.assistant-message')].pop()
  return {
    streaming: document.querySelector('.composer textarea')?.disabled,   // 生成中判据，不要看发送按钮
    answer: last?.querySelector('.message-content').textContent.trim(),
    chips: [...last.querySelectorAll('.tool-chip')].map(c => c.textContent.trim()),
    sources: [...last.querySelectorAll('.citations button')].map(b => b.textContent.trim()),
    notice: document.querySelector('.notice')?.textContent ?? null,
  }
}
```

新建课程走 `window.prompt`，Browser pane 没有对话框处理器，先 `window.prompt = () => '课程名'` 再点。

### 1.4 课程、教材与锚点

| 课程 | 教材文件 | 来源 | 锚点事实（切片内页码） |
| --- | --- | --- | --- |
| 大语言模型 | `llm-微调-LoRA.pdf` | 复旦《大规模语言模型：从理论到实践》 | QLoRA 用 NF4 把权重量化到 4-bit、双重量化、分页优化器（p4–p6） |
| 大语言模型 | `llm-指令数据集.pdf` | 同上，表 5.1 | Super-NaturalInstructions：500 万实例 / 1616 任务 / 55 种语言 / 手动构建（p1、p6） |
| 深度学习 | `深度学习-批量规范化.pdf` | 《动手学深度学习》 | 批量规范化置于仿射变换与激活函数之间（p1、p3）；Ali Rahimi 2017 NeurIPS 炼金术之喻（p7–p8） |
| 操作系统 | `os-cpu-scheduling.pdf` | OSTEP 第 7 章（英文） | Round Robin 响应时间好、周转时间差（p6–p7） |
| 机器学习数学 | `math-gaussian.pdf` | Mathematics for Machine Learning（英文） | 两个高斯密度的乘积仍是高斯，C=(A⁻¹+B⁻¹)⁻¹（p8） |

OCR 提问用 `教材页-提问.png`（OSTEP 第 7 页的真实排版页，含 Figure 7.6/7.7）。

## 2. 主轮次

每步都给出**期望**，任一期望不成立即记为失败并停下定位，不要跳过继续。

1. **打开首页** `http://127.0.0.1:5173`
   期望：状态栏 `● connected`、`deepseek/deepseek-v4-flash`、`retrieval: hybrid_bge`；侧栏无课程。

2. **新建 4 门课程**：`大语言模型`、`深度学习`、`操作系统`、`机器学习数学`
   期望：每门课程带稳定色点；创建后自动切进该课程工作区。

3. **上传教材**：进「知识仓库」，按 1.4 把 5 份切片上传到对应课程（`input[accept*=".pdf"]`）
   期望：上传后自动起索引 job，流水线依次亮起「解析 → 切块 → 向量 → 索引」；结束后状态为「已索引」，
   副标题显示 `N 块 · 语义 + 词面检索就绪`。五份合计约 225 块。

4. **检索验证**：在「大语言模型」检索 `QLoRA`，在「操作系统」检索 `turnaround`
   期望：结果给出资料名 + `p.X` + 排序分，页码与 1.4 的锚点页对得上。

5. **刷新页面**
   期望：课程、资料、索引状态、会话、消息、引用、每轮解析结果全部还在。

6. **课程会话问锚点事实**：「大语言模型」会话问 *"QLoRA 相比 LoRA 多做了什么来省显存？"*
   期望：出现工具 chip「✓检索教材 · 命中 N 段」并在本轮结束后仍然可见；回答给出 **NF4 4-bit 量化 /
   双重量化 / 分页优化器**；`SOURCES` 指向 `llm-微调-LoRA.pdf` 的 p4–p6。

7. **点引用**
   期望：右侧抽屉显示页码与教材原文片段，不是空占位。

8. **跨文档引用**：同一会话问 *"Super-NaturalInstructions 这个指令集有多大、什么语言、怎么构建的？"*
   期望：回答给出 500 万 / 多语言 / 手动构建；引用可跨两份资料，编号与正文 `[n]` 一一对应。

9. **引用只列用到的**：任一课程会话问一个覆盖多个知识点的问题
   期望：`SOURCES` 的编号集合等于回答正文里出现的 `[n]` 集合（允许有空洞，如 1、2、4、5、6），
   没被引用的检索结果不出现在列表里。

10. **跨语言检索**：「操作系统」用中文问 *"时间片轮转为什么响应时间好、周转时间反而差？"*；
    「机器学习数学」问 *"两个高斯密度乘起来还是高斯分布吗？"*
    期望：中文问题命中英文教材，引用落在 `os-cpu-scheduling.pdf:6~7`、`math-gaussian.pdf:8`；
    trace 里应看到模型自主把关键词换成英文再查一次。

11. **课程隔离 + 教材外兜底**：留在「操作系统」会话问 *"QLoRA 是怎么省显存的？"*
    期望：引用里不出现任何 `llm-*.pdf`；回答先说明本课程资料没有，再以「以下不是当前教材结论：」
    开头给通用知识；因为没有引用教材，`SOURCES` 为空。

12. **模型自主多轮工具**：「大语言模型」会话问 *"这门课有哪些资料？其中关于指令数据集的部分讲了什么？"*
    验证：

    ```bash
    tail -n 3 data/e2e/traces/*.jsonl | .venv/bin/python -c "import sys,json;[print(r.get('status'), 'rounds=%s' % r.get('tool_rounds'), [(t['origin'],t['name'],t['ok']) for t in r.get('tools',[])]) for l in sys.stdin if l.strip().startswith('{') for r in [json.loads(l)]]"
    ```

    期望：`status=completed`、`tool_rounds ≥ 1`，`tools` 里既有 `seed` 也有 `model`。

13. **计划 / 档案工具与骨架页**：问 *"我这门课的学习计划和学习记录现在是什么状态？"*
    期望：chip 出现「学习计划 · 暂无计划」「学习档案 · 档案为空」，回答如实说没有；侧栏
    `03 学习计划` / `04 学习档案` 显示对应空态。

14. **图片提问**：「操作系统」会话上传 `教材页-提问.png`，正文写 *"这页在讲哪几种调度算法？"*
    期望：发送前 attach chip 显示 OCR 转录（能看到 SJF / Round Robin）；回答基于转录作答并带
    p.7 引用；发送后附件区清空。这一步会产生一次真实 Qwen-OCR 调用。

15. **通用模式解析**：切「通用模式」新建会话问 *"大语言模型这门课里，指令数据集一般怎么构建？"*
    期望：顶部「本轮解析到：大语言模型」，会话色点变课程色，引用来自该课程。
    再新建一个通用会话问 *"帮我复习一下"*
    期望：「本轮未解析到课程」，固定澄清话术，无引用、无工具 chip。
    （同一会话里追问会沿用上一轮解析，测未解析必须换新会话。）

16. **课程名嵌套**：临时新建 `深度学习进阶`，在新的通用会话问 *"深度学习进阶这门课的重点是什么？"*
    期望：解析到「深度学习进阶」（`reason=explicit_course_name`），不是 ambiguous。

17. **Wiki**：任一课程「知识仓库 → Wiki 知识页」
    期望：默认「Wiki 尚未启用」；打开开关后列出已索引资料；点「解析到 Wiki」立即显示阶段与进度，
    完成后显示「Wiki 已生成」，按钮变「重新解析到 Wiki」。

18. **坏教材**：上传一个下载被截断的 PDF
    期望：索引 job 落到 `failed`，错误信息「未能从教材中提取可检索文本」，不产生垃圾索引。

19. **索引期间的检索延迟**：上传整本教材（`data/e2e-fixtures/source/d2l-zh.pdf`，813 页），
    在 job 处于 `embedding` 阶段时连续打几次 `POST /courses/{id}/knowledge/search`
    期望：中位数在秒级以内（当前实测中位 1.5s、最坏 3.0s；空闲时 30ms）。若退化到几十秒，
    说明向量模型的分段持锁被改回了整批持锁。

20. **收尾检查**
    - `read_console_messages`：整轮无 error。
    - trace 命令：每轮都有记录，没有意外的 `status=failed`。
    - 「管理与设置 → 检查服务」：provider/model 真实、检索方式「语义 + 词面混合」、migration 版本正常。

## 3. 补充轮次：供应商失败降级（可选，约 2 分钟）

停掉实例，用坏 Key 重启，再问一次第 6 步的问题：

```bash
TEXT_API_KEY=sk-invalid STORAGE_DATA_DIR=data/e2e ./scripts/dev.sh
```

期望：出现 `provider_fallback` 后由本地 responder 完成回答，回答带明确的本地标识，会话仍正常持久化；
状态栏与 health 如实显示降级。

## 4. 这条测试不覆盖

`session_busy` 并发抢同一会话、`client_request_id` 幂等重放、`stream_interrupted`（需要在流中途切断
供应商）、100 MiB 上限校验、飞书渠道。这些留给后端测试与手工验证。

## 5. 验收项对照

| 交付计划 §5 验收项 | 步骤 |
| --- | --- |
| 1 启动与健康检查 | 1 |
| 2 默认通用模式、可切课程 | 1、2、15 |
| 3 会话列表色点与顶部标签 | 15 |
| 4 上传 → 索引 → 检索验证 | 3、4 |
| 5 通用会话解析 + 引用 | 15 |
| 6 模糊问题不全库检索 / 课程不越界 | 11、15 |
| 7 知识仓库先选课程、Wiki 默认关闭 | 3、17 |
| 8 刷新后状态仍在 | 5 |
| 9 工具只接受服务端 scope | 11（边界由 `tests/backend/test_module_boundaries.py` 保证） |
| 10 构建与测试 | `./scripts/check.sh`（本测试之外单独跑） |
| 11 远端模型真实 provider + 同轮完成解析/引用/持久化 | 6、8、20 |
| 工具循环 | 12、13 |
| 引用与答案一致 | 9、11 |
| 课程解析健壮性 | 15、16 |
| 索引与对话并发 | 19 |
