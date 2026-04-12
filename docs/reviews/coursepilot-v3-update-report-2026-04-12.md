# CoursePilot v3 项目更新报告（2026-04-12）

## 1. 概要

本次 v3 升级已经从“规划阶段”进入“骨架落地 + 主链路接管”阶段。

当前 `v3/architecture-upgrade` 分支已经完成以下关键转变：

- `Router` 保持原名，并升级为主 Planner，输出 `PlanPlusV1`
- `SessionStateV1` 成为服务端短期会话状态真源
- `BaseAgent + 继承式上下文策略` 已接入 `Router / Tutor / QuizMaster / Grader`
- `ExecutionRuntime + TaskGraphV1` 已成为 `/chat` 与 `/chat/stream` 的统一执行入口
- `RAGService / MemoryService / WorkspaceStore / TelemetryService / EventBus / ToolHub` 已从 Runner 中抽离并接入主链路
- 工具调用统一收口到 `ToolHub -> MCP stdio`

这意味着 v3 第一阶段的核心架构目标已经基本达成，系统不再完全依赖“Runner 内硬编码串流程”的旧形态。

## 2. 已完成的核心升级

### 2.1 状态模型与 Agent 结构

- 在 `backend/schemas.py` 中补齐了 `PlanPlusV1`、`SessionStateV1`、`AgentContextV1`、`ToolDecision`、`ToolAuditRecord`
- `ChatRequest / ChatResponse` 已支持 `session_id`、`resolved_mode`、`current_stage`
- `BaseAgent` 已提供统一的 `build_context()`、`call_tool()`、telemetry 接口
- 各 Agent 通过继承实现差异化上下文策略，没有额外引入独立 `ContextManager` 顶层服务

### 2.2 SessionState 真源落盘

- `WorkspaceStore` 已支持 `load_session_state / save_session_state`
- `SessionState` 当前按课程写入：
  `data/workspaces/<course_name>/sessions/<session_id>.json`
- 会话恢复顺序已经调整为：
  `state.session_state -> 内存缓存 -> workspace JSON -> history 中旧 internal meta`

### 2.3 Runtime / TaskGraph

- `ExecutionRuntime` 已统一接管 sync/stream 主入口
- `TaskGraphV1` 已显式建模以下步骤：
  `plan_intent`
  `prefetch_rag`
  `prefetch_memory`
  `build_agent_context`
  `detect_submission`
  `run_tutor / run_quiz / run_exam / run_grade`
  `persist_session_state / persist_records / persist_memory`
  `synthesize_final`
- `prefetch_rag` 与 `prefetch_memory` 已并行执行
- `STRICT_NEW_RUNTIME=1` 已可用于暴露 fallback，而不是让 fallback 静默掩盖问题

### 2.4 服务层与工具治理

- `RAGService`：管理课程级 retriever 加载与检索
- `MemoryService`：管理 profile context、历史记忆预取、learn/practice/exam 写记忆
- `WorkspaceStore`：管理 session、practice、exam、mistake 文件落盘
- `TelemetryService`：管理 fallback 等 trace 事件
- `EventBus`：统一 `__status__ / __citations__ / __context_budget__ / __tool_calls__`
- `ToolHub`：统一权限、preflight、dedup、idempotency、audit

## 3. 验证结果

当前分支已通过以下固定回归：

- `C:\Users\10423\miniconda3\envs\study_agent\python.exe -m unittest tests.test_contract_fixes tests.test_mcp_stdio`
- `C:\Users\10423\miniconda3\envs\study_agent\python.exe tests\test_basic.py`

当前新增或补强的验证点包括：

- `session_id` 可从 workspace JSON 恢复
- `Router` 的 `resolved_mode` 改判可被 Runtime 遵循
- `TaskGraph` 编译与路由契约正确
- `AgentContextV1` 可被 Router 等 Agent 正常产出
- `ToolHub` 权限与幂等 key 基本可用
- `EventBus` 产出的隐藏事件形状与旧前端兼容
- `tests.test_mcp_stdio` 已改为 `unittest`，固定命令不再出现“0 tests”假通过

## 4. 代码审阅结论

### 4.1 主要优点

- 架构升级方向与既定讨论保持一致，没有偏离到“自由自治多 Agent”
- `Router`、`SessionState`、`Runtime` 三条主线已经真正接上，而不是停留在概念层
- 这次拆分尽量保持了 `/chat`、`/chat/stream` 与三模式的兼容
- 测试覆盖面比升级前更完整，尤其是契约与 MCP stdio 基线

### 4.2 发现的问题

#### 1. 流式 fallback 路径会重复发送最终隐藏 meta

- 位置：
  `core/runtime/executor.py:796-834`
  `core/runtime/executor.py:835-849`
  `core/orchestration/runner.py:1231-1237`
- 问题：
  当 `execute_stream()` 在新 Runtime 路径中失败后，会清掉 runtime control 并回退到 legacy stream。
  但 legacy stream 本身已经会发送一次 `__tool_calls__`，Runtime 在 fallback 分支结束后又会再发送一次统一的 final hidden meta。
- 影响：
  前端可能重复接收到最终 `session_state/history_summary_state/quiz_meta/exam_meta`。
- 结论：
  这是当前最值得优先修复的行为缺陷。

#### 2. TaskGraph 对 learn 模式的记忆写入建模不完整

- 位置：
  `core/runtime/executor.py:419-423`
  `core/orchestration/runner.py:763-772`
- 问题：
  `run_learn_mode()` 在用户显式要求“记住”时，会在 runtime-managed 模式下登记 `persist_memory` effect；
  但 `compile_taskgraph()` 只在 `route == run_grade` 时加入 `persist_memory` 步骤。
- 影响：
  实际副作用已经发生，但 `taskgraph_statuses` 不会反映 learn 分支的记忆落盘，造成状态图与真实行为不一致。
- 结论：
  这是一个观测与审计层面的不一致，优先级低于功能性 bug，但建议尽快补齐。

### 4.3 其他观察

- `tests.test_mcp_stdio` 在模拟缺失 server 时仍会产生 `ResourceWarning`，目前不影响通过，但说明 `_StdioMCPClient` 失败收尾还可以更干净。
- benchmark 指标已经补入脚本，但本轮尚未实际跑完整基准数据，因此性能结论仍待补证。

## 5. 当前状态判断

如果把 v3 第一阶段目标定义为“主架构收口完成，并具备可验证的运行链路”，当前状态可以判定为：

- **架构目标：已基本完成**
- **主链路接线：已完成**
- **兼容与验证：已完成基础版**
- **严格意义的收尾：仍有少量尾项**

更准确地说，系统已经从“方案实施”进入“收尾与稳态化”阶段。

## 6. 后续建议

建议下一轮工作按下面顺序推进：

1. 修复 stream fallback 下的重复 hidden meta 发送问题
2. 让 TaskGraph 对 learn 分支的 `persist_memory` 建模与真实行为一致
3. 跑一轮 smoke benchmark，补齐：
   `fallback_rate`
   `resolved_mode_override_count`
   `taskgraph_route`
   `session_store_hit_rate`
4. 若 benchmark 稳定，再进入下一阶段功能优化，而不是继续大规模重构
