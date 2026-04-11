# CoursePilot 架构升级实施计划（2026-04-10）

> 本计划基于根目录中的《CoursePilot 项目审阅与架构升级方案.pdf》与当前仓库代码实现共同整理，目标是把“审阅意见”收敛成一份可落地、可分 PR 执行、可回滚的正式改进计划。

## 1. 执行摘要

CoursePilot 当前已经不是“聊天前端 + 单轮 RAG”型项目，而是一个围绕课程学习闭环构建的工程系统：前端使用 Streamlit，后端使用 FastAPI，核心编排由 `OrchestrationRunner` 负责，执行层由 `Router / Tutor / QuizMaster / Grader` 四个专职 Agent 协作，底层串联 RAG、长期记忆、MCP 工具和评测体系。

这次升级的主方向是正确的，尤其是：

- 将能力层从 Runner 中逐步解耦，RAG、记忆系统、工具系统相互解耦合，统一接口
- 强化 Router，使其从“模式判定器”升级为“主 Planner”
- 引入显式运行时与共享状态
- 将当前固定 workflow 演进为“主 Agent 产计划，Runtime 执行计划”

但结合当前代码现状，升级不能走“大爆炸重写”路线，而应遵循以下原则：

- 保持 `/chat`、`/chat/stream` 和 learn / practice / exam 三模式兼容
- 保留“中心编排 + 专职 Agent”边界，不切到完全自治多 Agent
- 主 Agent 只负责决策与约束，不直接掌控副作用执行
- RAG、Memory、Tool 仍是主路径能力，不是临时兜底插件
- 先完成状态显式化和能力层抽象，再引入 TaskGraph Runtime
- 每个阶段都能 benchmark 对比、独立回滚、单独审阅

## 2. 方案评审结论

### 2.1 合理且建议保留的方向

#### 2.1.1 分层架构方向正确

将系统拆为交互层、主 Agent 层、专项 Agent 层、能力层、状态层、记忆层，与当前仓库的真实复杂度匹配。当前项目已经隐含了这些层，只是边界还不够清晰。

#### 2.1.2 “主 Agent 决策，专项 Agent 执行”适合当前项目

当前 Router 已经能够产出结构化计划字段：

- `need_rag`
- `style`
- `output_format`
- `question_raw`
- `user_intent`
- `retrieval_keywords`
- `retrieval_query`
- `memory_query`

这说明 Router 已经具备向 Planner 演进的基础，只是目前还没有输出正式的执行计划。

#### 2.1.3 显式 SessionState 非常必要

当前状态散落在：

- `history_summary_state`
- `quiz_meta`
- `exam_meta`
- history 中的 internal meta
- workspace 文件
- `memory.db`

引入统一的 `SessionStateV1` 能明显降低状态演进的复杂度，也更利于调试与回归测试。

#### 2.1.4 Capability / Service 层抽象有高收益

当前 Runner 内部承担了过多横切逻辑：

- RAG 预取
- Memory 预取
- 上下文组装与裁剪
- 工具上下文透传
- 记录落盘
- 记忆写入
- trace 与元事件组织

将这些抽成 `RAGService / MemoryService / ContextAssembler / ToolHub / WorkspaceStore / Telemetry` 能显著降低耦合。

#### 2.1.5 TaskGraph / Executor 适合作为中长期执行载体

当前 learn / practice / exam 三模式流程其实已经具有明确步骤，只是这些步骤写死在 Runner 里。把它们抽象成受约束的执行图，比直接让 LLM 控制流程更稳。

### 2.2 需要修正后再实施的点

#### 2.2.1 不建议把“主 Agent 生成计划”理解为完全自由执行

不应允许 LLM 输出任意步骤并直接驱动全流程。当前项目存在大量确定性边界：

- submission 检测
- practice / exam 评分分流
- 文件落盘
- 记忆写入
- 工具权限控制

这些步骤必须继续由 Runtime 控制，不能让模型自由生成。

#### 2.2.2 不建议把 RAG / Memory 降级为“困难场景才调用”

这与当前产品形态不符。CoursePilot 的 learn / practice / exam 都默认依赖教材检索，Memory 也已经是长期状态的重要组成。它们应继续是主路径能力，而不是临时插件。

#### 2.2.3 不建议让 Specialist Agent 直接拿全量原始上下文

当前项目已经证明：

- history 是最容易膨胀的上下文来源
- 工具轮消息会快速放大 token
- RAG 全文和原始 tool result 直接回灌会污染决策

因此即使未来引入 Runtime，也必须继续保留分层上下文工程，不能让 Worker 直接消费 raw state。

#### 2.2.4 当前不需要扩增 Agent 数量

当前 4 个 Agent 已经覆盖主场景。当前瓶颈在于：

- Planner 不够强
- Runtime 不够显式
- 能力层未抽离
- 状态模型不统一

而不是 Agent 数量不够。

## 3. 目标架构设计

### 3.1 目标分层

建议把目标架构固定为 6 层：

1. **Interaction Layer**
   - Streamlit + FastAPI
   - 负责请求接入、SSE、workspace 选择、历史透传

2. **Planner Layer**
   - 从 `RouterAgent` 升级为 `LeadAgent`
   - 输出 `PlanPlusV1`
   - 负责任务意图、query rewrite、能力选择、输出约束、replan 建议

3. **Runtime Layer**
   - 从 `OrchestrationRunner` 演进为 `ExecutionRuntime`
   - 负责读取 `PlanPlusV1`、编译 `TaskGraphV1`、调度 Service 与 Specialist Agent、控制副作用

4. **Specialist Agent Layer**
   - `TutorWorker`
   - `QuizWorker`
   - `GraderWorker`
   - 只接收 `AgentContextV1`，不直接操作持久化和全局状态

5. **Capability / Service Layer**
   - `RAGService`
   - `MemoryService`
   - `ContextAssembler`
   - `ToolHub`
   - `WorkspaceStore`
   - `Telemetry`

6. **State / Memory Layer**
   - `SessionStateV1`
   - workspace 文件
   - `memory.db`
   - raw facts / summary state / prompt injection view

### 3.2 新增核心内部对象

#### 3.2.1 `SessionStateV1`

统一承载以下状态：

- `course_name`
- `mode`
- `history_summary_state`
- `last_quiz`
- `last_exam`
- `question_raw`
- `user_intent`
- `retrieval_query`
- `memory_query`
- `permission_mode`
- `idempotency_keys`
- `last_taskgraph_digest`

迁移期通过 internal meta 兼容读取旧字段。

#### 3.2.2 `PlanPlusV1`

在当前 `Plan` 基础上扩展，固定包含：

- `task_type`
- `need_rag`
- `allowed_tools`
- `style`
- `output_format`
- `question_raw`
- `user_intent`
- `retrieval_keywords`
- `retrieval_query`
- `memory_query`
- `capabilities`
- `risk_level`
- `permission_mode`
- `replan_policy`

#### 3.2.3 `TaskGraphV1`

第一版只允许白名单步骤：

- `prefetch_rag`
- `prefetch_memory`
- `build_context`
- `detect_submission`
- `run_tutor`
- `run_quiz`
- `run_exam`
- `run_grade`
- `persist_meta`
- `persist_records`
- `persist_memory`
- `synthesize_final`

#### 3.2.4 `AgentContextV1`

作为 Specialist Agent 的只读上下文快照，包含：

- budgeted context
- citations
- selected memory snippets
- tool handle
- session snapshot
- constraints
- output format

### 3.3 默认设计决策

#### Planner / Runtime / Worker 分工

- Planner 负责“做什么”
- Runtime 负责“怎么执行”
- Worker 负责“怎么在本领域生成内容”

#### Replan 策略

- 默认最多一次 replan
- 带副作用步骤不允许自由 replan
- 后续通过 `idempotency_key` 保证即使重试也不会重复写入

#### RAG / Memory 策略

- learn / practice / exam 继续维持主路径预取
- `retrieval_query` 继续服务教材检索
- `memory_query` 继续服务长期记忆预取和 `memory_search`

#### 上下文策略

继续保留当前有效方案：

- rolling summary
- tool round slimming
- final rehydrate

架构升级只改变职责归属，不改变当前验证有效的策略本身。

#### 工具权限策略

- `websearch`、`filewriter` 等高风险工具纳入 `permission_mode`
- ToolHub 成为唯一入口
- 保持 `mcp_stdio` 为唯一主路径，不恢复本地 fallback

## 4. 分阶段实施计划

### PR-1：SessionState 正规化

#### 目标

先解决“状态散落”，不改业务流程。

#### 关键改动

- 在 `backend/schemas.py` 新增 `SessionStateV1`
- Runner 统一提取、标准化、回填 SessionState
- 兼容读取旧的：
  - `quiz_meta`
  - `exam_meta`
  - `history_summary_state`
- 输出时统一写回 `session_state` internal meta

#### 验收

- practice / exam 提交答案分流仍然正确
- rolling summary 仍能续用
- 前端不渲染 `session_state` 内容

### PR-2：MemoryService 与 ContextAssembler 抽离

#### 目标

从 Runner 中剥离最明显的横切逻辑。

#### 关键改动

- Memory 相关逻辑抽为 `MemoryService`
  - prefetch
  - search
  - write
  - `qa_summary` archive
  - profile update
- Context 逻辑拆为：
  - `ContextAssembler`
  - `ContextBudgeter`

#### 保持不变

- rolling summary
- tool slimming
- memory 注入策略

### PR-3：RAGService 与 WorkspaceStore 抽离

#### 目标

让 Runner 不再直接管理检索细节和文件系统副作用。

#### 关键改动

- 抽 `RAGService`
  - 输入固定为 `course_name + retrieval_query + mode + top_k`
- 抽 `WorkspaceStore`
  - 管 uploads / indexes / notes / practices / exams / mistakes

#### 验收

- 检索质量不变
- 练习/考试/错题记录路径不变
- 索引读写逻辑不回归

### PR-4：ToolHub 硬治理

#### 目标

把当前“软约束工具策略”升级成“硬治理入口”。

#### 关键改动

- 在现有 `ToolPolicy + MCP client` 基础上抽 `ToolHub`
- 引入：
  - `permission_mode = safe | standard | elevated`
  - `phase contract`
  - `risk_level`
  - `idempotency_key`
  - 审计字段

#### 验收

- 高风险工具在不同权限模式下行为符合预期
- 重复工具调用能正确 dedup
- 工具错误能进入 trace 和审计日志

### PR-5：LeadAgent + PlanPlus

#### 目标

把 Router 从“计划字段生成器”升级为“主 Planner”。

#### 关键改动

- `RouterAgent` 升级为 `LeadAgent`
- 输出升级为 `PlanPlusV1`
- 保留当前 rewrite 和意图识别逻辑
- 新增：
  - `capabilities`
  - `risk_level`
  - `permission_mode`
  - `replan_policy`

#### 保持不变

- 仍不直接编排步骤执行
- 仍不直接调用工具或写文件

### PR-6：TaskGraph / Executor 接管模式流程

#### 目标

把 Runner 的模式分支逻辑迁移为显式执行图。

#### 关键改动

- 新增 `TaskGraphV1`
- 新增 `Executor`
- 将 learn / practice / exam 从硬编码逻辑迁移为：
  - `PlanPlus -> TaskGraph -> Executor`
- `detect_submission` 保持确定性实现

#### 验收

- 用户可见行为不变
- 输出、评分、落盘、memory 更新全部等价

### PR-7：EventBus 与并发 prefetch

#### 目标

收敛 SSE 元事件协议，提升 TTFT 体感。

#### 关键改动

- 抽 `EventBus`
- 统一组织：
  - `__status__`
  - `__citations__`
  - `__context_budget__`
  - `__tool_calls__`
- 并行化：
  - RAG prefetch
  - memory prefetch

#### 验收

- 前端仍能正确消费现有 SSE 协议
- TTFT、E2E 不劣化
- trace 不丢 `request_id / first_chunk_ms / elapsed_ms`

### PR-8：依赖与测试体系整顿

#### 目标

为架构升级提供稳定回归基线。

#### 关键改动

- 统一依赖真源
- 区分：
  - `unit`
  - `integration`
  - `external-key dependent tests`
- benchmark、trace_case、delta_report 纳入固定回归流程

#### 验收

- 默认环境下测试不会因外部 key 缺失而误失败
- benchmark 可持续输出 before / after 对比

## 5. 验收与回归标准

### 5.1 功能一致性

- learn / practice / exam 三模式核心路径保持不变
- 单题练习、多题练习、考试出卷、交卷批改、错题落盘全部正常

### 5.2 状态一致性

- `SessionStateV1` 能覆盖旧 meta 语义
- 旧历史能自动迁移读取
- 新历史只依赖 `session_state` 即可继续运行

### 5.3 执行一致性

- `PlanPlus -> TaskGraph -> Executor` 的运行结果与当前 Runner 等价
- `detect_submission` 不受 Planner 漂移影响
- Replan 不会导致重复副作用

### 5.4 工具治理一致性

- `safe / standard / elevated` 行为清晰可测试
- `idempotency_key` 能阻止重复副作用写入
- ToolHub 统一进入 trace / audit

### 5.5 性能与质量一致性

每个 PR 之后都用现有 benchmark 检查：

- TTFT
- E2E
- avg prompt tokens
- duplicate_tool_call_rate
- tool_success_rate
- `hit@k`
- `top1_acc`
- `precision@k`

原则：

> 架构更清晰不能以质量明显退化为代价。

## 6. 风险与默认取舍

### 6.1 主要风险

- Router 过度升级为自由控制器，导致流程失控
- 新状态模型与旧 internal meta 兼容期过长，导致双轨维护
- Service 抽象过早，接口不稳，反而增加复杂度
- Executor 引入后，模式边界和副作用边界被打散

### 6.2 默认取舍

- 先做状态和服务，再做 Runtime
- 先做受约束 TaskGraph，不做自由 DSL
- 不新增更多 Agent
- 不改变外部 API
- 不动当前已验证有效的上下文策略

## 7. 结论

这次升级不应被定义为“把 CoursePilot 改成另一个自由 Agent 框架”，而应定义为：

> 在保留当前产品语义和稳定边界的前提下，把现有 CoursePilot 从“中心 Runner + 多处横切逻辑”升级为“LeadAgent + ExecutionRuntime + Specialist Workers + Capability Services + SessionState”的工程化架构。

这条路线与当前仓库实现连续、风险可控、收益明确，也最适合作为后续几轮 PR 的主线。
</proposed_plan>
