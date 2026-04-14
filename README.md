# CoursePilot

面向大学课程学习场景的 Multi-Agent + RAG + MCP 系统，强调教材证据可追溯、练习/考试闭环，以及可复现的评测与治理能力。

CoursePilot 不是一个“套了教材检索的聊天机器人”。它更像一个可持续演进的学习系统：

- 外部仍是 `learn / practice / exam` 三种模式
- 内部通过 `workflow_template` 控制执行路径，避免自由编排失控
- 练习和考试走 `artifact-first`，先生成结构化题目，再评分与落档
- 工具调用经过 `ToolHub` 做权限、预算、去重、幂等和审计
- 评测支持 `bench -> judge -> review` 全链路闭环

![CoursePilot UI](docs/images/启动界面.png)

## Why This Project

- 教材证据可追溯：检索结果带来源和页码，回答不是“黑盒生成”。
- 学习闭环完整：从概念讲解，到练习、考试、评分、记录沉淀都在同一系统内完成。
- 多 Agent 但不失控：`Router + Tutor + QuizMaster + Grader` 分工明确，同时由模板和运行时收口。
- 工具治理完整：不是简单 function calling，而是带预算、权限、审计和失败处理的工具链。
- 会话状态有真源：`SessionState` 持久化为服务端短期状态，不靠 history 硬猜。
- 动态评测可复现：支持 benchmark、judge、review 三段式回归，而不只看主观回答效果。

## Quick Start

### 1. 安装依赖

推荐使用 Python `3.11`。

```bash
py -3.11 -m pip install -r requirements.txt
```

### 2. 配置 `.env`

在项目根目录创建 `.env`，最少需要：

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
DEFAULT_MODEL=gpt-4o-mini
```

如果需要 judge 模型、运行治理参数或完整环境变量说明，请看 [配置总览](docs/guides/config-overview.md)。

### 3. 启动系统

```bash
# 终端 1：后端
py -3.11 -m backend.api

# 终端 2：前端
streamlit run frontend/streamlit_app.py
```

默认后端端口 `8000`，前端端口 `8501`。

### 4. 上传教材并构建索引

推荐直接通过前端完成：

1. 创建或选择课程
2. 上传教材文件
3. 点击“构建索引”
4. 开始学习 / 练习 / 考试

如果你需要批量重建所有课程索引：

```bash
py -3.11 scripts/rebuild_indexes.py
```

## How It Works

对外，CoursePilot 提供三种学习模式：

- `learn`：解释概念、回答问题、引用教材
- `practice`：生成练习题并评分讲评
- `exam`：生成试卷并完成一次性评卷

对内，系统不会直接把模式映射到单一函数，而是先由 `Router` 生成计划，再进入受控模板执行。当前核心执行思路是：

- `workflow_template`：决定本轮是“只讲解”、“只出题”还是“出题后评分”等路径
- `ExecutionRuntime + TaskGraph`：把计划编译成可观测、可回退的执行步骤
- `RAGService + MemoryService`：负责教材证据和历史记忆预取
- `ToolHub`：统一工具门控与审计

更详细的执行链路见 [架构文档](docs/guides/architecture.md)。

## Repository Layout

```text
backend/       FastAPI API 层
core/          编排、运行时、Agent、服务层
frontend/      Streamlit UI
rag/           文档解析、切块、检索与索引
memory/        长期记忆与画像
mcp_tools/     MCP 工具客户端与本地工具
scripts/       索引、评测与辅助脚本
benchmarks/    基准数据与 gold
docs/          架构、配置、使用、评测与内部资料
data/          本地工作区、索引和运行产物
```

脚本入口说明见 [scripts/README.md](scripts/README.md)。  
如果你想看一个轻量演示入口，可以看 [docs/examples/demo.py](docs/examples/demo.py)。

## Read More

- [架构说明](docs/guides/architecture.md)
- [配置总览](docs/guides/config-overview.md)
- [使用手册](docs/guides/usage.md)
- [评测手册](docs/guides/evaluation.md)
- [贡献说明](docs/guides/contributing.md)

## FAQ

### 它和普通 RAG 问答项目有什么不同？

重点不只是“把文档喂给模型”，而是把教材证据、练习/考试链路、状态管理、工具治理和动态评测放进同一个工程体系里。

### 为什么内部还有 `workflow_template`？

因为它能把外部简单模式映射成受控执行模板，让多 Agent 系统更稳定、可测、可演进。

### 可以只把它当教材问答系统来用吗？

可以。你完全可以只用 `learn` 模式；练习、考试和评测链路是增量能力，不强制依赖。

## License

MIT
