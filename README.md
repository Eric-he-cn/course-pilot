# CoursePilot — 大学课程学习 Agent（Multi-Agent + RAG + MCP）

CoursePilot 是一个面向大学课程学习场景的多 Agent 学习系统，目标是把“教材证据 + 练习/考试闭环 + 评测可验证”落成可持续演进的工程化系统。

本仓库体现的是 v3 架构：`Router` 负责规划模板，`ExecutionRuntime + TaskGraph` 负责白名单执行，`SessionState` 负责全局短期状态真源，`ToolHub` 统一工具治理，`bench -> judge -> review` 形成动态评测闭环。

---

## 核心能力

- 课程级 RAG：教材入库、分块、混合检索、引用来源可追溯
- 多 Agent 编排：Router + Tutor + QuizMaster + Grader 的职责链
- 工作流模板化：仅允许受支持的 `workflow_template`，避免自由图失控
- artifact-first：练习/考试先生成结构化 artifact，再渲染展示与评分
- 工具治理：ToolHub 统一权限、预算、去重、幂等、审计
- 会话真源：SessionState JSON 持久化，跨轮恢复与回写
- 生命周期治理：Session TTL 自动清理 + 手动会话清理 API
- 在线影子评测：前端会话级开关，异步写队列并后台评测
- 动态评测：bench -> judge -> review 全链路可复现评估

---

## 系统架构概览

```
User
  ↓
API /chat
  ↓
OrchestrationRunner（兼容入口）
  ↓
ExecutionRuntime + TaskGraph（模板执行器）
  ├─ RouterAgent -> PlanPlusV1（workflow_template + action_kind）
  ├─ RAGService / MemoryService 预取
  ├─ Agent.build_context() 选择并组织上下文
  ├─ Tutor / QuizMaster / Grader 执行
  ├─ ToolHub -> MCP stdio 工具调用
  └─ WorkspaceStore / SessionState / Memory 持久化
```

更详细说明请见 `docs/guides/architecture.md`。

---

## 快速开始

### 1) 安装依赖

```bash
pip install -r requirements.txt
```

### 2) 配置环境变量

在项目根目录创建 `.env`，至少包含：

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
DEFAULT_MODEL=gpt-4o-mini
```

可选：评测用 Judge 配置（与主模型解耦）

```bash
EVAL_JUDGE_API_KEY=...
EVAL_JUDGE_BASE_URL=https://api.deepseek.com
EVAL_JUDGE_MODEL=deepseek-chat
EVAL_JUDGE_TEMPERATURE=0
```

可选：v3 运行治理参数

```bash
RAG_COMPRESSION_MODE=adaptive   # adaptive|always|off
SESSION_TTL_DAYS=30
MEMORY_EPISODES_SOFT_CAP=2000
MEMORY_EVICT_BATCH_SIZE=200
ONLINE_EVAL_WORKER_ENABLED=1
```

### 3) 启动后端与前端

```bash
python -m backend.api
```

```bash
streamlit run frontend/streamlit_app.py
```

默认后端端口 `8000`，前端端口 `8501`。

---

## 数据准备

推荐通过前端完成流程：

1. 创建/选择课程
2. 上传教材文件（PDF/TXT/MD/DOCX/PPTX/PPT）
3. 点击构建索引

命令行方式可使用：

```bash
python rebuild_indexes.py
```

---

## 模式与工作流模板

对外兼容的模式仍为 `learn/practice/exam`，但内部以 `workflow_template` 执行：

- `learn_only`
- `practice_only`
- `exam_only`
- `learn_then_practice`
- `practice_then_review`
- `exam_then_review`

`Router` 只能在模板集合中选择，`ExecutionRuntime` 校验模板前置条件并编译 `TaskGraph`。

练习与考试采用 artifact-first：

- QuizMaster 生成 `PracticeArtifactV1/ExamArtifactV1`
- validator 通过后才渲染题面
- Grader 只消费 artifact，不再从 history 反推题目

---

## 测试与评测

基础测试：

```bash
python -m unittest discover -s tests -p "test*.py"
python tests/test_basic.py
```

动态评测建议顺序：

```bash
python scripts/perf/bench_runner.py --cases benchmarks/smoke_contract.jsonl --gold benchmarks/rag_gold_v2.jsonl --output-dir data/perf_runs/smoke
python scripts/eval/judge_runner.py --raw data/perf_runs/smoke/baseline_raw.jsonl --cases benchmarks/smoke_contract.jsonl --output-dir data/perf_runs/smoke_judge
python scripts/eval/review_runner.py --benchmark-summary data/perf_runs/smoke/baseline_summary.json --benchmark-raw data/perf_runs/smoke/baseline_raw.jsonl --judge-summary data/perf_runs/smoke_judge/judge_summary.json --judge-raw data/perf_runs/smoke_judge/judge_raw.jsonl --output-dir data/perf_runs/smoke_review
```

完整说明见 `docs/guides/evaluation.md`。

---

## 目录结构

```
backend/           # FastAPI API 层
core/              # 编排、运行时、Agent、服务层
rag/               # 文档解析、切块、检索与索引
memory/            # 长期记忆与画像
mcp_tools/         # MCP 客户端/服务端与本地工具
frontend/          # Streamlit UI
scripts/           # bench/judge/review 与辅助脚本
benchmarks/        # 测试集与 gold
data/              # 运行时数据与评测产物（本地）
docs/              # 架构、评测、配置与评审记录
```

---

## 常见问题

1. 为什么 `general` 不再是正式模式？
`general` 仅作为旧输入兼容字段保留，内部会被归一化为 6 个模板之一。

2. 为什么 practice/exam 不直接评分？
评分需要稳定结构，artifact-first 可以避免坏 JSON 与空试卷导致的链路不稳定。

4. 有会话清理能力吗？
有。后端提供：
- `DELETE /workspaces/{course_name}/sessions/{session_id}`
- `POST /workspaces/{course_name}/sessions/cleanup`

前端也提供“清理过期会话”和“删除当前会话”入口。

5. 在线影子评测会不会影响主响应？
不会。影子评测是异步队列模式，主链路仅追加队列写入，不阻塞 `/chat` 与 `/chat/stream`。

3. Judge 可以不用 DeepSeek 吗？
可以。Judge 与主模型解耦，只需配置 `EVAL_JUDGE_*` 环境变量。

---

## 开发说明

- 架构与数据流详见 `docs/guides/architecture.md`
- 配置总览详见 `docs/guides/config-overview.md`
- 评测手册详见 `docs/guides/evaluation.md`
- 贡献流程详见 `docs/guides/contributing.md`

---

## License

MIT License
