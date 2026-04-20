# CoursePilot 关键配置总览

更新时间：2026-04-19
适用范围：当前主干代码（`core/`, `rag/`, `memory/`, `mcp_tools/`, `backend/`, `frontend/`, `scripts/`）  
说明：本清单按“模块 -> 参数 -> 默认值/行为”整理，便于定位配置归属与运行行为。密钥类信息不写明文。

---

## 1. 运行与接口层

| 参数 | 默认值 | 作用 |
|---|---|---|
| `API_HOST` | `0.0.0.0` | FastAPI 监听地址 |
| `API_PORT` | `8000` | FastAPI 端口 |
| `API_BASE` | `http://localhost:8000` | Streamlit 前端请求后端地址 |
| `API_RELOAD` | `0` | 是否开启 Uvicorn 自动重载（开发态可设为 `1`） |
| `DATA_DIR` | `./data/workspaces` | 课程工作区与 `SessionState` 文件根目录 |
| `SSE_HEARTBEAT_SEC` | `8` | `/chat/stream` 心跳状态推送间隔 |
| `CONTEXT_BUDGET_EVENT_TIMEOUT_SEC` | `3.0` | 前端预算事件超时提示阈值 |
| `SESSION_TTL_DAYS` | `30` | SessionState 过期清理天数 |
| `SESSION_CLEANUP_ENABLED` | `1` | 独立 session cleanup worker 是否执行清理逻辑 |
| `SESSION_CLEANUP_INTERVAL_SEC` | `900` | session cleanup worker 扫描间隔（秒） |
| `EMBEDDING_PRELOAD_ON_STARTUP` | `1` | 后端启动时主动预热 embedding 模型，减少首个请求冷启动 |

补充：
- 推荐统一使用 Python `3.11` 作为开发、测试与评测解释器。
- 当前 API 进程启动时只做 `workspace restore + embedding/rerank preload`，不再自动拉起后台 worker。
- 独立 worker 入口：
  - `python -m scripts.workers.session_cleanup_worker`
  - `python -m scripts.workers.shadow_eval_worker`

---

## 2. LLM 接口层

| 参数 | 默认值 | 作用 |
|---|---|---|
| `OPENAI_API_KEY` | 无 | LLM API 密钥 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `DEFAULT_MODEL` | 环境变量 | 对话模型名（由运行环境决定） |

补充：
- 流式调用中会优先请求 `stream_options.include_usage=true`；不支持时自动降级重试。
- token 统计口径：`usage` 优先，缺失时回退 `prompt_tokens_est`。

---

## 3. ReAct 工具调用（Act/Synthesize）

| 参数 | 默认值 | 作用 |
|---|---|---|
| `ACT_MAX_ROUNDS` | `4` | Act 工具推理轮数上限 |
| `ACT_MAX_TOKENS` | `160` | 每个 Act 轮 `max_tokens` 上限 |
| `ALWAYS_FINAL_STREAM` | `1` | 是否始终保留最终单独流式回答轮 |
| `TOOL_RETRY_MAX` | `1` | 工具重试上限（仅支持一次重试策略） |
| `TOOL_DEDUP_MIN_INTERVAL_MS` | `1500` | 重复工具调用最小间隔判定 |
| `STRICT_NEW_RUNTIME` | `0` | 开启后禁止 Runtime fallback，任何兼容回退都视为失败 |
| `TOOL_ROUND_FULL_CONTEXT_ROUNDS` | `1` | 前几轮保留全量上下文 |
| `TOOL_ROUND_KEEP_LAST_TOOL_MSGS` | `2` | 工具轮保留最近 tool 消息数 |
| `TOOL_ROUND_RAG_SUMMARY_MAX_TOKENS` | `400` | 工具轮 RAG 摘要预算 |
| `TOOL_ROUND_MEMORY_SUMMARY_MAX_TOKENS` | `180` | 工具轮 memory 摘要预算 |
| `TOOL_FINAL_REHYDRATE` | `1` | 最终轮是否回灌上下文 |
| `TOOL_FINAL_REHYDRATE_MODE` | `summary` | 回灌粒度（`summary/full`） |
| `TOOL_STREAM_TEXT_CHUNK_CHARS` | 动态 | 工具轮文本分块流式输出字符数 |
| `TOOL_STREAM_TEXT_CHUNK_DELAY_MS` | `12.0` | 工具轮文本分块推送延迟 |

---

## 4. 上下文管理器（ContextBudgeter）

### 4.1 总预算与分段预算

| 参数 | 默认值 | 作用 |
|---|---|---|
| `CTX_TOTAL_TOKENS` | `8192` | 上下文总预算 |
| `CTX_SAFETY_MARGIN` | `256` | 安全余量 |
| `CB_HISTORY_RECENT_TURNS` | `5` | 最近保留的原文对话轮数 |
| `CB_RECENT_RAW_TURNS` | `5` | 原文保留轮数（配合摘要） |
| `CB_HISTORY_RECENT_RAW_TURNS` | 回退到 `CB_HISTORY_RECENT_TURNS` | Runner 侧会话滚动摘要保留的最近原文轮数；未单独设置时沿用历史轮数配置 |
| `CB_HISTORY_SUMMARY_MAX_TOKENS` | `2000` | 历史摘要预算 |
| `CB_RAG_MAX_TOKENS` | `1800` | RAG 段预算 |
| `CB_MEMORY_MAX_TOKENS` | `450` | memory 段预算 |
| `CB_RAG_SENT_PER_CHUNK` | `2` | 每块保留句数 |
| `CB_RAG_SENT_MAX_CHARS` | `120` | 句级压缩单句最大长度 |
| `CB_MEMORY_TOPK` | `2` | memory 注入条数 |
| `CB_MEMORY_ITEM_MAX_CHARS` | `100` | 单条 memory 注入长度 |
| `RAG_COMPRESSION_MODE` | `adaptive` | RAG 压缩模式（`adaptive/always/off`） |
| `RAG_ADAPTIVE_PRESSURE_THRESHOLD` | `0.9` | `adaptive` 下触发压缩的上下文压力阈值 |

### 4.2 历史压缩（LLM 条件触发）

| 参数 | 默认值 | 作用 |
|---|---|---|
| `CB_ENABLE_LLM_HISTORY_COMPRESS` | `1` | 开启历史 LLM 压缩 |
| `CB_LLM_COMPRESS_TRIGGER_TOKENS` | `600` | 触发阈值 |
| `CB_LLM_COMPRESS_TARGET_TOKENS` | `260` | 目标压缩长度 |
| `CB_LLM_COMPRESS_TIMEOUT_MS` | `1200` | 压缩超时 |
| `CB_LLM_COMPRESS_MAX_RETRIES` | `0` | 压缩重试次数 |
| `CB_LLM_COMPRESS_MODEL` | 空 | 压缩模型（空=沿用主模型） |
| `CB_LLM_COMPRESS_TEMPERATURE` | `0.1` | 历史压缩温度 |
| `CONTEXT_LLM_COMPRESSION_THRESHOLD` | `0.9` | 仅当上下文压力达到阈值时才触发昂贵 LLM 压缩 |
| `CB_DISABLE_LLM_HISTORY_COMPRESS_MODES` | `practice,exam` | 这些模式默认禁用历史 LLM 二次压缩 |

### 4.3 滚动摘要（Rolling Summary）

| 参数 | 默认值 | 作用 |
|---|---|---|
| `CB_HISTORY_SUMMARY_BLOCK_TURNS` | `5` | 每次滚动压缩的历史轮数 |
| `CB_HISTORY_SUMMARY_MAX_BLOCKS` | `10` | 最多保留的历史摘要块数量 |
| `CB_HISTORY_BLOCK_COMPRESS_TARGET_TOKENS` | `160` | 单个摘要块目标长度 |
| `CB_HISTORY_BLOCK_COMPRESS_MAX_TOKENS` | `220` | 单个摘要块最大长度 |
| `CB_HISTORY_BLOCK_COMPRESS_TIMEOUT_MS` | `1500` | 单个摘要块压缩超时 |
| `CB_HISTORY_BLOCK_COMPRESS_TEMPERATURE` | `0.1` | 单个摘要块压缩温度 |

### 4.4 结构控制

| 参数 | 默认值 | 作用 |
|---|---|---|
| `CB_INCLUDE_RAW_HISTORY_IN_MESSAGES` | `0` | 是否原文注入历史 |
| `RAG_COMPRESS_OWNER` | `retriever` | RAG 压缩责任方（`retriever/budgeter`） |

---

## 5. RAG 全流程参数

### 5.1 切块

| 参数 | 默认值 | 作用 |
|---|---|---|
| `CHUNK_SIZE` | `512` | 字符级切块大小 |
| `CHUNK_OVERLAP` | `50` | 切块重叠长度 |
| `CHUNK_STRATEGY` | `chapter_hybrid` | 切块策略（`fixed/chapter_hybrid`） |

说明：
- `chapter_hybrid` 支持 `第X章 / Chapter X / Markdown 标题` 识别，失败自动回退 `fixed`。
- chunk 元信息包含：`doc_id/page/chunk_id/chapter/section`。

### 5.2 检索与融合

| 参数 | 默认值 | 作用 |
|---|---|---|
| `RETRIEVAL_MODE` | `hybrid` | `dense/bm25/hybrid` |
| `TOP_K_RESULTS` | `3` | 兜底 top-k |
| `RAG_TOPK_LEARN_PRACTICE` | `4` | learn/practice top-k |
| `RAG_TOPK_EXAM` | `8` | exam top-k |
| `BM25_K1` | `1.5` | BM25 参数 |
| `BM25_B` | `0.75` | BM25 参数 |
| `HYBRID_RRF_K` | `60` | RRF 融合系数 |
| `HYBRID_DENSE_WEIGHT` | `1.0` | dense 权重 |
| `HYBRID_BM25_WEIGHT` | `1.0` | bm25 权重 |
| `HYBRID_DENSE_CANDIDATES_MULTIPLIER` | `3` | dense 候选扩展倍数 |
| `HYBRID_BM25_CANDIDATES_MULTIPLIER` | `3` | bm25 候选扩展倍数 |
| `RERANK_ENABLED` | `1` | 是否为 `learn/practice` 启用 Cross-Encoder 二阶段精排 |
| `RERANK_CANDIDATES_LEARN_PRACTICE` | `12` | `learn/practice` 进入 rerank 的 fused candidate 数量 |
| `RAG_EVIDENCE_DENSE_MIN` | `0.40` | 证据准入的最低向量相似度门槛 |
| `RAG_EVIDENCE_BM25_MIN` | `1.0` | 证据准入的最低 BM25 原始分门槛 |
| `RAG_EVIDENCE_MAX_FUSED_RANK` | `4` | 只允许融合排序前 N 的 chunk 进入证据集 |

实现细节：
- 向量库为 `FAISS IndexFlatL2`（当前未采用 IVF/PQ/HNSW）。
- rerank 当前仅覆盖 `learn/practice`，链路为 `dense + bm25 -> RRF -> Cross-Encoder rerank -> evidence gate`。

---

## 6. Embedding 与索引构建

| 参数 | 默认值 | 作用 |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-base-zh-v1.5` | 嵌入模型 |
| `EMBEDDING_DEVICE` | `auto` | `cpu/cuda/auto` |
| `EMBEDDING_BATCH_SIZE` | 按设备自适应 | 嵌入批大小 |
| `EMBEDDING_PRELOAD_ON_STARTUP` | `1` | 服务启动时是否预加载嵌入模型 |
| `RERANK_MODEL` | `BAAI/bge-reranker-base` | Cross-Encoder rerank 模型 |
| `RERANK_DEVICE` | `auto` | `cpu/cuda/auto` |
| `RERANK_BATCH_SIZE` | 按设备自适应 | rerank 批大小 |
| `RERANK_PRELOAD_ON_STARTUP` | `1` | 服务启动时是否预加载 reranker |
| `DATA_DIR` | `./data/workspaces` | 课程工作区根目录 |

---

## 7. MCP 工具系统

| 参数 | 默认值 | 作用 |
|---|---|---|
| `MCP_PYTHON_BIN` | 当前解释器 | MCP 子进程 Python |
| `SERPAPI_API_KEY` | 无 | `websearch` 工具密钥 |
| `MCP_INPROCESS_FASTPATH` | `0` | 是否为低风险本地工具启用进程内快路径 |

关键机制：
- 工具链路固定：`OpenAI tool_call -> ToolHub -> MCPTools.call_tool -> stdio MCP -> server_stdio`。
- MCP 客户端超时默认 `20s`，失败自动重启重试一次。
- 默认不做本地直调 fallback；仅当 `MCP_INPROCESS_FASTPATH=1` 时，对少量低风险本地工具开放进程内快路径。
- 权限模式固定为 `safe / standard / elevated`。

---

## 8. 记忆系统（SQLite + FTS5）

| 参数 | 默认值 | 作用 |
|---|---|---|
| `MEMORY_DB_PATH` | `./data/memory/memory.db` | 记忆库路径 |
| `MEMORY_SEARCH_BACKEND` | `fts5` | 检索后端（失败回退 `like`） |
| `MEMORY_DEDUP_ENABLE` | `1` | request 级去重开关 |
| `MEMORY_DEDUP_SCOPE` | `request` | 去重作用域 |
| `MEMORY_DEDUP_MAX_ENTRIES` | `64` | request 缓存上限 |
| `MEMORY_SEARCH_IN_ACT_DEFAULT` | `0` | Act 轮默认禁用 memory_search |
| `MEMORY_QA_RETAIN_RECENT` | `50` | 最近保留的原始 `qa` 数量 |
| `MEMORY_QA_ARCHIVE_BATCH` | `20` | 每批归档为一个 `qa_summary` 的 `qa` 数量 |
| `MEMORY_QA_ARCHIVE_MAX_IMPORTANCE` | `0.55` | 参与归档的 `qa` 重要度上限 |
| `MEMORY_QA_SUMMARY_TARGET_TOKENS` | `220` | 单条 `qa_summary` 目标长度 |
| `MEMORY_QA_SUMMARY_MAX_TOKENS` | `320` | 单条 `qa_summary` 最大长度 |
| `MEMORY_EPISODES_SOFT_CAP` | `2000` | episodes 软上限（触发 LRU-like 淘汰） |
| `MEMORY_EVICT_BATCH_SIZE` | `200` | 单次淘汰批大小 |
| `MEMORY_EVICT_ENABLE` | `1` | 是否启用异步淘汰 |
| `MEMORY_EVICT_PROTECT_IMPORTANCE` | `0.8` | 高重要度保护阈值 |

数据模型：
- `episodes`：`qa/qa_summary/mistake/practice/exam`
- `user_profiles`：`weak_points/concept_mastery/avg_score/...`
- 记忆元数据支持 `mode/agent/phase` 过滤。

---

## 9. Router 与 Replan

| 参数 | 默认值 | 作用 |
|---|---|---|
| `ENABLE_ROUTER_REPLAN` | `1` | 是否启用重规划 |
| `ENABLE_STRUCTURED_OUTPUTS_ROUTER` | `1` | Router 优先使用 strict json_schema 输出 |
| `ROUTER_PLAN_RETRY_ON_PARSE_FAIL` | `1` | Router 解析/校验失败后是否自动重试一次 |
| `REPLAN_MIN_CHARS` | `160` | 低质量判定阈值（字符） |
| `REPLAN_MIN_SENTENCES` | `2` | 低质量判定阈值（句数） |

补充：
- Replan 最多发生一次。
- 只允许发生在任何 `persist_*` 副作用步骤之前。

### 9.1 Router v3 规划字段（非环境变量）

以下字段由 `PlanPlusV1` 在运行时生成与消费，不通过 `.env` 配置：

- `workflow_template`: `learn_only / practice_only / exam_only / learn_then_practice / practice_then_review / exam_then_review`
- `action_kind`: `learn_explain / practice_generate / practice_grade / exam_generate / exam_grade / learn_then_practice`
- `route_confidence`
- `route_reason`
- `required_artifact_kind`
- `tool_budget`
- `allowed_tool_groups`
- `tool_policy_profile`
- `context_budget_profile`

执行语义：`ExecutionRuntime` 优先根据 `workflow_template + action_kind` 编译 `TaskGraphV1`，`task_type` 保留为兼容字段。

当前默认 profile 映射：

- `learn_only` -> `tool_policy_profile=learn_readonly`，`context_budget_profile=learn_standard`
- `practice_only` -> `practice_generate`，`practice_standard`
- `learn_then_practice` -> `practice_generate`，`learn_then_practice`
- `practice_then_review` -> `grading_restricted`，`grading_compact`
- `exam_only` -> `exam_locked`，`exam_standard`
- `exam_then_review` -> `exam_locked`，`grading_compact`

### 9.2 ToolHub 硬限制参数

| 参数 | 默认值 | 作用 |
|---|---|---|
| `ACT_MAX_TOOLS_PER_REQUEST` | 未设置（profile / plan 未给出时兜底） | 单请求工具调用总上限 |
| `ACT_MAX_TOOLS_PER_ROUND` | 未设置（profile / plan 未给出时兜底） | 单轮工具调用上限 |

补充：
- `tool_policy_profile` 内置的 `tool_budget` 是 ToolHub 硬上限。
- `plan.tool_budget` 只能在 profile 上限内进一步收紧，不能把 `per_request_total / per_round / per_tool` 抬高。
- `per_tool` 合并规则是：profile 已声明的工具取 profile 与 plan 的更严格值；profile 未声明的工具可由 plan 补充限制。
- `ACT_MAX_TOOLS_PER_REQUEST / ACT_MAX_TOOLS_PER_ROUND` 主要用于无 profile 或历史兼容路径的兜底，不应突破 profile 硬上限。
- 命中上限会在 trace 中打点：`tool_total_cap_hit_count / per_tool_cap_hit_count / tool_round_cap_hit_count`。
- ToolHub 仍保留 `permission_mode + phase gate + dedup + idempotency + audit`。
- ToolHub 当前内置 profile：
  - `learn_readonly`
  - `practice_generate`
  - `grading_restricted`
  - `exam_locked`
- 工具拒绝会返回统一字段：
  - `success=false`
  - `failure_class=denied`
  - `denied_reason=<具体原因>`

### 9.3 Memory LRU-like 行为

`episodes` 新增 `last_accessed_at`：

- 命中检索时会 touch 更新时间。
- 检索排序按 `importance DESC + last_accessed_at DESC + created_at DESC`。
- 归档/淘汰优先淘汰“低 importance 且最久未访问”记录，而不是纯 `created_at`。

---

## 10. Structured Outputs 灰度

| 参数 | 默认值 | 作用 |
|---|---|---|
| `ENABLE_STRUCTURED_OUTPUTS_QUIZ` | `0` | Quiz strict schema 开关 |
| `ENABLE_STRUCTURED_OUTPUTS_EXAM` | `0` | Exam strict schema 开关 |
| `ENABLE_STRUCTURED_OUTPUTS_GRADER` | `0` | Grader strict schema 开关 |

策略：
- 开启：优先 strict schema；
- 失败：自动回退旧 JSON 修复链，不阻断主流程。

---

## 11. 评测脚本参数（`scripts/perf/bench_runner.py`）

补充说明：
- 当前根目录 `benchmarks/` 默认只保留 active canonical 集与 gold 流水线文件：`cases_v1.jsonl`、`rag_gold_v1.jsonl`、`gold_candidates.jsonl`、`gold_manual_fix.jsonl`、`gold_rejected.jsonl`、`gold_label_sessions.jsonl`。
- 历史广覆盖 benchmark / gold 套件已归档到 `benchmarks/archive/20260415_legacy_reset/`。
- `bench_runner.py` 的默认参数仍然指向 canonical 集；如果要做覆盖率 lint 或历史大回归，需要显式指向 archive 目录或自建 case 集。
- 推荐使用 `python -m scripts.eval.run smoke/full/review` 作为统一入口。该入口会优先使用非空 active 文件；当 root active 文件为空时，会回退到 `benchmarks/archive/20260415_legacy_reset/` 的对应 canonical / smoke 基线。
- `dataset_lint --path benchmarks` 当前用于 broad lint 口径；若根目录没有可 lint 的 active case，会回退到归档 `v3_expanded_84.jsonl`，不要把它解读为 canonical RAG headline。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--cases` | `benchmarks/cases_v1.jsonl` | 用例集 |
| `--gold` | `benchmarks/rag_gold_v1.jsonl` | RAG gold |
| `--output-dir` | `data/perf_runs/baseline_v1` | 输出目录 |
| `--profile` | `baseline_v1` | 评测 profile |
| `--repeats` | `2` | 每条重复次数 |
| `--recompute-only` | `false` | 只重算已有 `baseline_raw.jsonl` 的 RAG 指标，不重跑模型 |
| `--gold-min-coverage` | `0.5` | case_id 与 gold 的最小覆盖率阈值 |
| `--gold-mismatch-policy` | `fail` | 覆盖率不足时 `warn/fail` |
| `--gate-policy` | `fail` | 基准门禁失败时 `warn/fail/off` |

支持：
- checkpoint 断点续跑（按 `case_id#repeat` 去重）。
- 输出 `raw/summary/md/checkpoint`。
- v3 指标补充：`fallback_rate`、`resolved_mode_override_count`、`taskgraph_route`、`session_store_hit_rate`。
- v3 上下文指标补充：`avg_history_tokens`、`avg_rag_tokens`、`avg_memory_tokens`、`avg_final_context_tokens`、`avg_input_context_tokens`、`avg_context_pressure_ratio`。
- v3 trace contract：当 `requires_citations/need_rag=true` 且 stream 最终有 citations 时，若缺少 `retrieval / retrieval_missing_index / retrieval_skipped` 事件，会标记 `trace_contract_error=true`。

RAG 命中判定策略（按优先级）：
- `gold_chunk_ids` -> `chunk_id`
- `gold_doc_ids + gold_pages` -> `doc_page`
- `gold_doc_ids` -> `doc_id`
- `gold_pages` -> `page`
- `gold_keywords` -> `keyword`

输出会附带：
- `rag_match_strategy`
- `rag_match_signal`
- `gold_case_total/gold_case_matched/gold_case_coverage`

补充脚本：
- `python -m scripts.eval.run smoke`
- `python -m scripts.eval.run full`
- `python scripts/eval/dataset_lint.py --path benchmarks`
- `python scripts/eval/judge_runner.py --raw data/perf_runs/<profile>/baseline_raw.jsonl --cases benchmarks/cases_v1.jsonl`
- `python scripts/eval/review_runner.py --benchmark-summary ... --benchmark-raw ... --judge-summary ... --judge-raw ...`
- `python scripts/eval/build_gold_candidates.py --run-all-suggestions --count 30`
- `python scripts/eval/review_gold_candidates.py`

Judge 独立配置：
- `EVAL_JUDGE_API_KEY`
- `EVAL_JUDGE_BASE_URL`
- `EVAL_JUDGE_MODEL`
- `EVAL_JUDGE_TIMEOUT_MS`
- `EVAL_JUDGE_TEMPERATURE`

Gold-screen judge 独立配置：
- `GOLD_SCREEN_API_KEY`
- `GOLD_SCREEN_BASE_URL`
- `GOLD_SCREEN_MODEL`

默认回退：
- 未单独设置 `GOLD_SCREEN_*` 时，会回退到 `OPENAI_API_KEY / OPENAI_BASE_URL / DEFAULT_MODEL_THINKING / DEFAULT_MODEL`

---

## 12. 在线影子评测（会话级异步）

| 参数 | 默认值 | 说明 |
|---|---|---|
| `ONLINE_EVAL_WORKER_ENABLED` | `0` | 是否启动在线影子评测后台 worker（默认关闭，显式开启） |
| `ONLINE_EVAL_POLL_SEC` | `30` | 队列轮询间隔（秒） |
| `ONLINE_EVAL_RUN_JUDGE_REVIEW` | `1` | 是否自动触发 judge/review 子流程 |
| `ONLINE_EVAL_PYTHON_BIN` | 空（自动） | 在线评测子进程解释器 |
| `ONLINE_EVAL_JUDGE_TIMEOUT_SEC` | `1800` | `judge_runner` 超时 |
| `ONLINE_EVAL_REVIEW_TIMEOUT_SEC` | `900` | `review_runner` 超时 |

行为说明：
- 前端会话可开启 `shadow_eval`，后端将请求/响应写入 `data/perf_runs/online_eval/<date>/eval_queue.jsonl`。
- worker 异步消费队列并生成：
  - `benchmark_raw_online.jsonl`
  - `benchmark_summary_online.json`
  - `judge/*`
  - `review/*`

---

## 13. 当前本地 `.env`（脱敏快照）

已设置（示例）：
- `OPENAI_BASE_URL=https://api.deepseek.com`
- `DEFAULT_MODEL=deepseek-chat`
- `EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5`
- `EMBEDDING_DEVICE=auto`
- `EMBEDDING_BATCH_SIZE=256`
- `CHUNK_SIZE=600`
- `CHUNK_OVERLAP=120`
- `TOP_K_RESULTS=6`
- `DATA_DIR=./data/workspaces`
- `API_HOST=0.0.0.0`
- `API_PORT=8000`

密钥项（脱敏）：
- `OPENAI_API_KEY`
- `SERPAPI_API_KEY`

---

## 14. 面试建议：最常被问的“为什么这么配”

- `ACT_MAX_TOKENS=160`：限制工具轮冗余长文本，减少中间轮耗时。
- `ALWAYS_FINAL_STREAM=1`：保证最终答案轮有可感知的 TTFT 与流式体验。
- `RAG_TOPK` 分模式：learn/practice 更偏精炼，exam 保留更高证据覆盖。
- `MEMORY_SEARCH_IN_ACT_DEFAULT=0`：避免工具轮重复检索造成 token/时延膨胀。
- `MEMORY_SEARCH_BACKEND=fts5`：在规模增长时优于纯 LIKE；仍保留 LIKE 兼容回退。
- `chapter_hybrid`：利用教材结构提升检索语义密度，同时保留 fixed 回退稳态。
