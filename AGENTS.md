# AGENTS.md

本文件面向进入 CoursePilot 仓库执行任务的 AI 代理。若本文与代码或最新文档冲突，以代码和 `docs/guides/` 下的最新文档为准。

## Project Snapshot

CoursePilot 是面向大学课程学习场景的 Multi-Agent + RAG + MCP 系统。

当前执行核心是：

- `OrchestrationRunner`：应用级入口、状态恢复、服务与 Agent 宿主
- `ExecutionRuntime + TaskGraph`：编译并执行受控工作流
- `Router / Tutor / QuizMaster / Grader`：分别负责规划、讲解、出题、评分
- `RAGService / MemoryService / ToolHub`：分别负责教材证据、长期记忆、工具治理

## Where To Start

先读这些文档：

- `README.md`：项目定位、最短启动路径
- `docs/guides/architecture.md`：当前真实架构和执行链路
- `docs/guides/config-overview.md`：环境变量与运行参数
- `docs/guides/usage.md`：前端使用行为和用户可见能力
- `docs/guides/evaluation.md`：bench / judge / review 评测体系

常见代码入口：

- `backend/api.py`：FastAPI 路由、启动 bootstrap、SSE 入口
- `core/runtime/executor.py`：Runtime、TaskGraph、模板执行
- `core/orchestration/runner.py`：编排入口、SessionState 恢复、业务执行宿主
- `core/services/tool_hub.py`：工具权限、预算、去重、审计
- `mcp_tools/client.py`：MCP client、本地工具封装、工具返回结构

## Repository Map

- `backend/`：API 层和 HTTP/SSE 接口
- `core/`：编排、运行时、Agent、服务层
- `frontend/`：Streamlit UI
- `rag/`：文档解析、切块、embedding、FAISS、BM25、检索融合
- `memory/`：SQLite 长期记忆、用户画像、情景记忆
- `mcp_tools/`：MCP stdio client/server 和工具实现
- `scripts/`：索引、评测、worker 和辅助脚本
- `benchmarks/`：active benchmark、gold、候选与归档数据
- `docs/`：架构、配置、使用、评测、审阅和内部资料
- `data/`：本地运行产物和工作区数据，不是主要实现代码

补充：

- `docs/examples/` 是示例入口
- `docs/internal/` 是内部笔记
- `docs/archive/` 是历史归档

## Execution Rules

- 优先保持 `/chat` 和 `/chat/stream` 的外部 schema 稳定。
- 不要轻易删除或改名 `SessionStateV1`、`PlanPlusV1`、`RequestContext` 的兼容字段。
- 修改 Router 规划时，同步考虑 `workflow_template / action_kind / tool_policy_profile / context_budget_profile`。
- 修改工具治理时，同步检查 `ToolHub + MCP + final synthesize failure summary`。
- 修改 RAG 或评测链路时，必须同步 benchmark 口径和相关文档。
- 修改前端历史或上下文行为时，保持“后端是语义真源”的原则；前端只做轻量安全上限。
- 不要随意引入大规模架构替换，例如分布式队列、ANN 索引、增量索引、远端 MCP server，除非任务明确要求。
- 不要把 `.env`、本地数据、缓存、测试临时目录或个人实验产物提交进仓库。

## Common Commands

推荐使用 Python `3.11`。
本机的conda环境是：study_agent，可以通过 `conda activate study_agent` 来激活。
启动系统：

```bash
py -3.11 -m backend.api
streamlit run frontend/streamlit_app.py
```

批量重建索引：

```bash
py -3.11 scripts/rebuild_indexes.py
```

核心回归：

```bash
py -3.11 -m unittest tests.test_contract_fixes tests.test_v3_priority_plan
```

评测入口：

```bash
py -3.11 -m scripts.eval.run smoke
py -3.11 -m scripts.eval.run full
py -3.11 -m scripts.eval.run review --benchmark-dir <dir> --judge-dir <dir>
```

独立 worker：

```bash
py -3.11 -m scripts.workers.session_cleanup_worker
py -3.11 -m scripts.workers.shadow_eval_worker
```

## Change Checklist

- 先阅读相关文档和入口代码，再修改实现。
- 改环境变量、默认行为或运行参数时，更新 `docs/guides/config-overview.md`。
- 改架构、数据流、执行链路或状态模型时，更新 `docs/guides/architecture.md`。
- 改前端用户可见行为时，更新 `docs/guides/usage.md`。
- 改 benchmark、judge、review 或 CI 评测入口时，更新 `docs/guides/evaluation.md`。
- 改工具调用行为时，检查 ToolHub 审计、失败语义、工具摘要和相关测试。
- 改 SessionState / Plan / RequestContext 时，保留旧数据和旧字段的兼容恢复路径。
- 改 RAG citation 或 evidence gate 时，确认前端引用显示、benchmark gold 口径和文档一致。

## Testing Expectations

默认优先跑：

```bash
py -3.11 -m unittest tests.test_contract_fixes tests.test_v3_priority_plan
```

涉及 MCP 或工具链时，额外检查相关工具回归；如果存在 `tests.test_mcp_stdio`，优先运行它。

涉及评测或 benchmark 时，至少运行 dataset lint：

```bash
py -3.11 -m scripts.eval.dataset_lint --path benchmarks --output-dir data/perf_runs/_lint
```

涉及脚本入口时，至少运行 `--help` 或 `py_compile` 做低成本检查。

## Docs To Update

- 架构 / Runtime / TaskGraph / 状态边界：`docs/guides/architecture.md`
- 配置 / 环境变量 / 默认值：`docs/guides/config-overview.md`
- 使用流程 / 前端表现 / 排障：`docs/guides/usage.md`
- 评测 / benchmark / judge / review：`docs/guides/evaluation.md`
- 脚本入口：`scripts/README.md`

## Compatibility Notes

- `PlanPlusV1` 继承并兼容旧 `Plan` 字段。
- `SessionStateV1` 需要兼容旧 `quiz_meta / exam_meta / history_summary_state`。
- `context / merged_context` 是 legacy 兼容字段；主 prompt 应优先使用 `history_context / rag_context / memory_context` 分栏。
- `RequestContext` 是请求级状态边界，避免跨请求共享可变状态。

