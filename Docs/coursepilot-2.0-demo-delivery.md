# CoursePilot 2.0 Demo 交付计划

本文是产品、架构、前端设计之外的实施契约，目标是在不牺牲模块边界的前提下交付可本地运行的端到端 Demo。

项目属性：个人开源项目，与任何公司内部 axon/mino、MR、SCM、发布平台或部署流程无关。本轮只允许本地实现、构建、自动化测试和端到端启动验证；远端推送、发布、部署及真实外部消息发送均不在授权范围内。

## 1. 本轮产品决策

### 1.1 通用模式与课程模式并存

- Web 默认进入“通用模式”，左侧课程切换器可进入任一课程工作区，也可随时回到通用模式。
- 会话创建时保存 `scope_mode=general|course`。课程会话固定 `course_id`；切换工作区不会修改旧会话，而是新建或续接目标工作区的会话。
- 通用会话不固定课程。每轮由 Course Resolver 根据明确课程名、材料引用、当前问题、近期上下文与候选课程匹配结果产生独立 `turn_course_context`；不确定时询问用户，不默默跨课程取证。成功结果只更新 `sessions.last_resolved_course_id` 列表投影，绝不写入 `sessions.course_id`。
- 飞书首版每个用户只有一个 `source=feishu, scope_mode=general` 的可见会话，数据库以唯一约束保证。用户通过自然语言确定课程；飞书不提供课程选择 UI，也不保存 `active_course_id`。
- 工具运行时只接受服务端产生的 `ResolvedCourseContext`；模型不能填写任意 `course_id`。通用模式改变的是解析入口，不放松数据边界。

### 1.2 会话列表

- 左栏始终存在会话列表，可查看、续接和新建会话。
- 每条会话显示标题、更新时间和课程色点。课程会话使用课程固定颜色；通用会话根据最近一次成功解析的课程显示对应色点，但保留通用模式图标。
- 尚未解析课程的通用会话使用中性灰；点击后在顶部显示本会话真实 scope 和当前推断课程。
- 隐藏 system session 与飞书内部投递 turn 不进入用户会话列表或未读计数。

### 1.3 知识仓库

- 导航中的“Wiki”统一更名为“知识仓库”。知识仓库包含同等重要的两个组成部分：`RAG 资料库` 与 `Wiki 知识页`。
- 默认打开 RAG 资料库，完整展示上传、解析、切块、嵌入、索引、检索验证和错误恢复；复用 1.0 RAG 能力时通过 adapter 接入。
- Wiki 是知识仓库中的可选视图，课程级默认关闭；用户开启后再选择教材解析到 Wiki。关闭 Wiki 不影响上传、索引、检索和 Tutor。

## 2. 模块边界

```text
HTTP / SSE / Web / Feishu adapters
                ↓
          Application use cases
                ↓
 courses | sessions | knowledge | agent | skills | learning | planning
                ↓
      public DTO / Protocol / typed event
                ↓
 SQLite | files | RAG | LLM | channel adapters
```

- `courses`：课程与稳定颜色，不了解会话、RAG 或 LLM 实现。
- `sessions`：通用/课程 scope、消息与列表投影，通过 `CourseResolverPort` 获取每轮课程上下文。
- `knowledge`：材料、索引 job、检索与可选 Wiki；RAG/Wiki 共用课程边界但互不调用内部实现。
- `agent`：组装提示词与工具循环，只依赖 ports；无模型配置时使用明确标识的 Demo responder。
- `channels`：Web 与飞书 adapter；飞书始终请求通用 session use case。
- 业务模块不得直接读取其他模块表或 import 其 repository/service；composition root 是唯一装配点。

## 3. Demo API 合约

| 方法 | 路径 | Demo 行为 |
| --- | --- | --- |
| `GET/POST` | `/api/v2/courses` | 列出/创建课程，返回稳定颜色 |
| `GET/POST` | `/api/v2/sessions` | 按 scope/workspace 列表或创建会话 |
| `GET` | `/api/v2/sessions/{id}/messages` | 获取持久化消息、解析课程和引用 |
| `POST` | `/api/v2/sessions/{id}/turns` | SSE 返回回复；通用模式先解析课程再检索 |
| `POST` | `/api/v2/courses/{id}/materials` | 上传教材并返回 material |
| `POST` | `/api/v2/materials/{id}/index` | 启动解析/切块/索引 Demo job |
| `GET` | `/api/v2/jobs/{id}` | 查询 job 阶段与进度 |
| `GET` | `/api/v2/courses/{id}/materials` | 展示 RAG 资料库与索引状态 |
| `POST` | `/api/v2/courses/{course_id}/knowledge/search` | 知识仓库中对用户明确选定的课程做检索验证；通用对话不调用此自由接口 |
| `PATCH` | `/api/v2/courses/{id}` | 修改 `wiki_enabled` 等课程设置 |
| `POST` | `/api/v2/materials/{id}/wiki` | 用户显式触发 Wiki Demo job |
| `GET` | `/api/v2/courses/{id}/plan` | 只读计划骨架：返回持久化计划或 `null`，写接口随规划功能开放 |
| `GET` | `/api/v2/courses/{id}/archive` | 只读档案骨架：返回证据事件计数与最近事件 |
| `GET` | `/api/v2/health` | 返回 DB、LLM 配置和 Demo fallback 状态 |

`SessionSummary` 最少包含：

```json
{
  "id": "session-id",
  "title": "链式法则怎么判断",
  "scope_mode": "general",
  "course_id": null,
  "resolved_course_id": "course-calculus",
  "course_name": "高等数学 II",
  "course_color": "#B56E3D",
  "source": "web",
  "updated_at": "2026-07-21T08:00:00+08:00"
}
```

通用会话当前 turn 的真实检索边界来自不可变记录，而不是上面的列表投影：

```text
turn_course_context:
  turn_id UNIQUE
  resolution_status = resolved | ambiguous | unresolved
  resolved_course_id NULL
  resolver_version
  reason
  created_at
```

SSE 稳定顺序为：

```text
turn_started      {request_id, session_id, scope_mode}
course_resolution {status, resolved_course_id?, course_name?, course_color?, reason}
tool_call         {call_id, name, arguments, origin}      # origin=seed 系统种子检索；origin=model 模型自主调用
tool_result       {call_id, name, ok, summary}
citation          {citation_id, document, page?, chunk_id, snippet, score}  # 工具命中的证据，跨工具去重编号
text_delta（流式增量）...
turn_completed | turn_failed
```

课程解析成功后，服务端先用用户问题做一次种子检索（`origin=seed`），其结果与历史一并注入首个模型请求；模型可在随后的多轮里自主发起更多 `tool_call`（`origin=model`），因此 `tool_call/tool_result` 可穿插在 `text_delta` 之间。达到工具轮次上限后不再下发工具。

供应商在输出任何增量前失败时发 `provider_fallback` 并切换本地 responder；已输出增量后中断则发 `stream_interrupted`，部分回答以 `interrupted` 状态持久化，随后以 `turn_failed` 结束，不静默重放。

只有 `course_resolution.status=resolved` 后才允许执行课程 RAG/Wiki/档案工具；`ambiguous/unresolved` 直接产生澄清回复。

## 4. Demo 技术范围

- Backend：Python 3.11+、FastAPI、标准库 SQLite、显式 migration、模块化目录、SSE。
- Frontend：React + TypeScript + Vite；服务端是语义真源，前端只维护 UI 状态。
- RAG Demo：支持 PDF/TXT/MD 上传、文本提取、切块和可检索索引。检索为混合召回：BGE 语义向量（`sentence-transformers`，与 1.0 相同的模型与查询前缀约定）+ SQLite FTS/词项，RRF 融合；embedding 依赖或模型不可用时自动退回纯词面检索，health 与 job 的 `retrieval_backend` 如实标注。
- LLM：已实现带工具调用的多轮 chat 内部合约、DeepSeek V4 Adapter 与本地 Demo Adapter。远端开关启用时，Agent 先以用户问题做种子检索，再让 `deepseek-v4-flash` 在带证据的多轮循环里按需自主调用检索/资料/计划/档案工具（关闭 thinking，带工具轮次与步数上限）；工具执行只接受服务端 `ResolvedCourseContext`，模型不能填写 `course_id`。未启用、课程未解析、没有证据或供应商失败时不伪造远端结果，health/SSE 明确报告 `provider`、`local_guardrail` 或 `demo_fallback`。provider 在首个增量前失败会发出 `provider_fallback` 后由本地 responder 完成本轮；已输出增量后中断发 `stream_interrupted`，不暴露 Key 或供应商响应正文。
- Wiki、Skill、掌握度和计划在 Demo 中覆盖主交互与接口骨架；不会为了展示效果写跨模块捷径。

## 5. 端到端验收

1. 启动一条命令可同时说明前后端启动方式，健康检查可见。
2. 默认进入通用模式，可通过左栏切换课程；切换不会篡改旧会话 scope。
3. 会话列表存在课程色点；通用会话经提问解析课程后更新色点和顶部标签。
4. 创建课程、上传资料、完成索引后，可在知识仓库看到解析阶段并执行检索验证。
5. 通用会话提问可解析到相关课程、检索材料、返回带资料名/页码或 chunk 的引用。
6. 通用会话的模糊问题不会触发全课程检索；课程会话不能读取其他课程资料。
7. 知识仓库默认打开 RAG，进入时必须明确选择课程；Wiki 默认关闭并且只有显式操作才构建。
8. 刷新页面后课程、会话、每轮课程解析、消息、材料和 job 状态仍在。
9. 架构测试或 import 检查能阻止模块直接依赖其他模块内部 service/repository，并阻止 Agent 工具绕过 `ResolvedCourseContext` 接收裸 `course_id`。
10. 前后端构建、后端测试与至少一条真实 HTTP 端到端 smoke 测试通过。
11. 远端模型启用时，health 报告真实 provider/model；端到端 smoke 必须证明课程解析、引用、DeepSeek 回答和消息持久化在同一轮完成。

## 6. 并行执行与审阅

- Backend implementer：数据库、模块接口、Course Resolver、材料/RAG、SSE 与测试。
- Frontend implementer：完整 React 界面、通用/课程切换、彩色会话列表、知识仓库与 API client。
- Integration owner（主 Agent）：维护本文契约、处理前后端接口差异、运行 Demo 与修复集成问题。
- Reviewer：实现完成后独立检查架构边界、数据隔离、错误语义、前端状态和端到端可运行性；阻塞项必须修复后再交付。
