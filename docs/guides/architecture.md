# CoursePilot 架构说明（v3 实现）

本文档描述当前代码真实实现的架构，不描述历史设计或废弃路径。若文档与代码冲突，以代码为准。

---

## 1. 设计目标

CoursePilot 的目标不是通用聊天，而是课程学习闭环：教材证据、练习/考试、评分讲评、记忆沉淀与评测可验证。

v3 的关键设计取舍：

- 模板化执行而非自由自治，保持稳定性
- SessionState 做全局短期状态真源
- Runtime 做治理与白名单执行
- Agent 负责自身生命周期内的上下文选择与响应组织
- 工具调用统一走 ToolHub 与 MCP stdio
- bench -> judge -> review 内建为动态审阅闭环

---

## 2. 端到端执行流程

```
User
  ↓
API /chat /chat/stream
  ├─ 可选：shadow_eval 队列写入（异步）
  ↓
OrchestrationRunner（兼容入口）
  ↓
ExecutionRuntime + TaskGraph（模板执行器）
  ├─ RouterAgent -> PlanPlusV1（workflow_template + action_kind）
  ├─ validate_template_preconditions
  ├─ RAGService / MemoryService 并行预取
  ├─ Agent.build_context() 选择与拼装最终上下文
  ├─ Tutor / QuizMaster / Grader 执行
  ├─ ToolHub -> MCP stdio 工具调用
  └─ WorkspaceStore / SessionState / Memory 持久化
```

Runtime 只负责治理与执行，最终上下文的选择权在 Agent。

---

## 3. 核心模块地图

| 模块 | 目录 | 作用 | 关键产物 |
|---|---|---|---|
| API | `backend/` | `/chat` 与 `/chat/stream` 接口 | `ChatResponse` / SSE |
| 编排入口 | `core/orchestration/` | 兼容旧入口与 SessionState 恢复 | Runtime 调用 |
| 运行时 | `core/runtime/` | TaskGraph 编译与执行 | 执行步骤与状态 |
| Agents | `core/agents/` | Router/Tutor/QuizMaster/Grader | 回答/题目/评分 |
| Services | `core/services/` | RAG/Memory/Workspace/Telemetry/EventBus/ToolHub | context/审计/落盘 |
| RAG | `rag/` | 解析、切块、检索 | citations |
| Memory | `memory/` | 长期记忆与画像 | memory snippets |
| MCP | `mcp_tools/` | stdio MCP client/server 与工具实现 | tool results |
| 评测 | `scripts/perf/` + `scripts/eval/` | bench/judge/review | raw/summary/report |

---

## 4. SessionState 与 Agent 上下文分层

### 4.1 SessionState（全局短期真源）

- 持久化路径：`data/workspaces/<course>/sessions/<session_id>.json`
- 负责跨 Agent 共享状态与恢复
- 生命周期治理：TTL 自动清理 + 手动清理 API
- 典型字段：
  - `task_full_text` / `task_summary`
  - `current_stage`
  - `active_practice` / `active_exam`
  - `latest_submission` / `latest_grading`
  - `metadata`（taskgraph_route / taskgraph_steps / fallback_events 等）

### 4.2 AgentContext（单 Agent 快照）

- Runtime 只提供 `PrefetchBundleV1` 的候选材料
- Agent 通过 `build_context()` 选择并组织最终上下文
- Agent 在生命周期内更新上下文，并通过 `StatePatchV1` 回写 SessionState

结论：SessionState 负责跨 Agent 的短期状态；Agent 只负责自身生命周期内的上下文管理与组织。

---

## 5. Workflow Templates

Router 只能从固定模板集合中选择：

- `learn_only`
- `practice_only`
- `exam_only`
- `learn_then_practice`
- `practice_then_review`
- `exam_then_review`

模板与 `action_kind` 的对应关系：

| workflow_template | action_kind | 说明 |
|---|---|---|
| learn_only | learn_explain | 仅讲解 |
| practice_only | practice_generate | 仅出题，不评分 |
| exam_only | exam_generate | 仅出卷，不评分 |
| learn_then_practice | learn_then_practice | 先讲解后出题 |
| practice_then_review | practice_grade | 对现有练习评分 |
| exam_then_review | exam_grade | 对现有试卷评分 |

### 前置条件

- `practice_then_review` 需要 `active_practice` 或可评分 artifact
- `exam_then_review` 需要 `active_exam` 或可评分 artifact
- 前置条件失败触发一次 Replan，仍失败则 fail-closed

---

## 6. Runtime + TaskGraph

Runtime 的责任：

- 编译白名单 TaskGraph
- 并行预取 RAG/Memory
- 调用对应 Agent
- 统一执行 `persist_*` 步骤
- 记录 taskgraph 状态与 fallback

Runtime 不负责：

- 直接拼装最终上下文
- 业务知识判断（交给 Agent）

---

## 7. Artifact-first 练习/考试链路

### 7.1 生成阶段

- QuizMaster 生成 `PracticeArtifactV1/ExamArtifactV1`
- ArtifactValidator 校验结构与最小可评分字段
- 通过后渲染题面
- 失败时 fail-closed，不回显坏 JSON

### 7.2 评分阶段

- Grader 只消费 artifact 与 submission
- 不再从 history 反推题面
- 评分结果落盘到 `latest_grading`

此链路的目标是稳定评分、避免空试卷与坏 JSON 漏出。

---

## 8. 工具治理（ToolHub）

统一入口：`OpenAI tool call -> ToolHub -> MCPTools.call_tool -> stdio MCP -> server_stdio`

治理能力：

- 权限模式：`safe / standard / elevated`
- 组门控：`allowed_tool_groups` 与工具组映射一致性校验
- 预算与上限：`per_request_total / per_round / per_tool`
- 去重与幂等：`dedup / idempotency`
- 记录审计：`ToolAuditRecord`
- 预算可见性：每轮注入 `tool_budget_snapshot`，Agent 可感知剩余额度

---

## 9. 记忆系统（LRU-like）

- 记忆存储在 `memory.db`（SQLite + FTS5）
- `episodes` 记录新增 `last_accessed_at`
- 淘汰策略：高 importance 保护，低 importance 按 `last_accessed_at` 淘汰

---

## 10. 数据目录布局

```
<data>/workspaces/<course>/
  ├─ uploads/
  ├─ indexes/
  ├─ notes/
  ├─ practices/
  ├─ exams/
  ├─ sessions/<session_id>.json
  └─ metadata.json
```

---

## 11. 评测系统位置

评测链路是系统的一部分，而不是外部插件：

- `bench_runner.py` 产出 raw/summary
- `judge_runner.py` 进行 LLM-as-judge
- `review_runner.py` 汇总回归与人工审阅队列
- 在线影子评测（会话级可选）：`OnlineShadowEvalService` 异步写队列并后台跑 `judge/review`

完整评测流程见 `docs/guides/evaluation.md`。

---

## 12. 兼容与弃用语义

- `general` 仅作为旧输入别名保留，内部会归一化到 6 个模板之一
- `task_type` 继续保留为兼容字段，Runtime 以 `workflow_template + action_kind` 为主
- 旧 `quiz_meta / exam_meta / history_summary_state` 仅用于恢复 SessionState

---

## 13. 关键配置索引

配置总览请见 `docs/guides/config-overview.md`，其中包含：

- LLM 与 RAG 参数
- ToolHub 预算与权限
- Router 与 Replan
- Memory LRU-like
- 评测脚本参数

---

## 14. 术语速查

- `SessionStateV1`: 服务端短期状态真源
- `PlanPlusV1`: Router 的结构化规划结果
- `workflow_template`: 模板化流程选择
- `action_kind`: 模板内动作语义
- `TaskGraphV1`: 白名单执行步骤
- `Artifact`: practice/exam 的结构化题目/试卷/评分载体

---

如需了解更底层实现细节，请直接从以下入口阅读代码：

- `core/runtime/executor.py`
- `core/agents/router.py`
- `core/agents/quizmaster.py`
- `core/agents/grader.py`
- `core/services/tool_hub.py`
