# 2026-04-06 仓库整理与上下文链路更新

本次更新聚焦三件事：仓库清洁、文档重组、上下文与记忆链路说明同步。

## 1. 仓库整理

- 将公开文档、审阅材料、工程笔记统一迁入 `docs/` 子目录：
  - `docs/guides/`
  - `docs/reviews/`
  - `docs/internal/notes/`
  - `docs/changelog/`
- `README.md` 保持在根目录，继续作为项目入口。
- 更新 `.gitignore`，显式忽略：
  - `__pycache__/`
  - `.pytest_cache/`
  - `pytest-cache-files-*/`
  - `tmp*/`
- 移除一处可静态确认、低风险的死代码：`OrchestrationRunner._trim_history_recent()`。

## 2. 上下文工程更新

### 2.1 Rolling Summary

- 历史对话不再采用“每次请求重算整段旧历史摘要”的方式。
- 当前实现改为：
  - 最近 `5` 轮原文始终保留
  - 每累计 `5` 轮旧历史，压成 1 个 `summary block`
  - 已生成的 block 不重复压缩
  - 最多保留 `10` 个 block，超出后淘汰最老 block
- 内部状态通过 `history_summary_state` 挂载到对话内部 meta，不改外部 API。

### 2.2 前端预算窗口修正

- 预算窗口中的 `history_len` 语义修正为“message 窗口”，不再暗示真实轮数。
- 当前展示会同时反映：
  - message 数
  - 估算 turns
  - rolling summary 是否命中
  - summary block 数量

## 3. 检索与 rewrite 更新

- Router 新增结构化 rewrite 字段：
  - `question_raw`
  - `user_intent`
  - `retrieval_keywords`
  - `retrieval_query`
  - `memory_query`
- rewrite 只作用于检索链路，不改用户原问题在回答链路中的语义。
- 当前分模式检索 top-k 为：
  - `learn/practice = 4`
  - `exam = 8`

## 4. 记忆系统更新

### 4.1 learn 模式默认不写普通情景记忆

- learn 普通问答不再默认写入 `qa`
- 仅在用户显式表达“记住 / 下次提醒 / 以后按这个偏好”等长期记忆意图时才写入

### 4.2 QA 归档压缩

- 旧 `qa` 不再长期堆积在主检索路径中
- 当前归档策略：
  - 最近保留 `50` 条原始 `qa`
  - 每批 `20` 条旧 `qa` 归档为 1 条 `qa_summary`
  - 高价值 `qa`（显式记忆请求或较高 importance）不参与归档
- 检索优先级为：
  - `mistake > practice > exam > qa_summary > qa`

## 5. 文档同步范围

本次已同步更新：

- `README.md`
- `docs/guides/usage.md`
- `docs/guides/architecture.md`
- `docs/guides/config-overview.md`
- `docs/guides/contributing.md`
- `docs/internal/notes/qa.md`
- `docs/internal/notes/debug.md`

重点修正内容：

- exam top-k 从旧口径 `6` 更新为当前口径 `8`
- `CB_HISTORY_RECENT_TURNS` 从旧口径更新为当前默认值 `5`
- 文档中“历史每次请求重算摘要”的描述更新为“rolling summary blocks”
- 文档中“QA 首行抽取归档”的描述更新为“批次压缩归档”

## 6. 备注

- 本次未引入 rerank
- 本次未加入 OCR / 图片 PDF 解析
- 外部 API（`/chat`、`/chat/stream`）保持不变
