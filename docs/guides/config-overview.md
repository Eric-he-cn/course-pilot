# CoursePilot 关键配置总览（面试版）

更新时间：2026-03-29  
适用范围：当前主干代码（`core/`, `rag/`, `memory/`, `mcp_tools/`, `backend/`, `frontend/`, `scripts/perf/`）  
说明：本清单按“模块 -> 参数 -> 默认值/行为”整理，便于面试问答。密钥类信息不写明文。

---

## 1. 运行与接口层

| 参数 | 默认值 | 作用 |
|---|---|---|
| `API_HOST` | `0.0.0.0` | FastAPI 监听地址 |
| `API_PORT` | `8000` | FastAPI 端口 |
| `API_BASE` | `http://localhost:8000` | Streamlit 前端请求后端地址 |
| `DATA_DIR` | `./data/workspaces` | 课程工作区与 `SessionState` 文件根目录 |
| `SSE_HEARTBEAT_SEC` | `8` | `/chat/stream` 心跳状态推送间隔 |
| `CONTEXT_BUDGET_EVENT_TIMEOUT_SEC` | `3.0` | 前端预算事件超时提示阈值 |

---

## 2. LLM 接口层

| 参数 | 默认值 | 作用 |
|---|---|---|
| `OPENAI_API_KEY` | 无 | LLM API 密钥 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容接口地址 |
| `DEFAULT_MODEL` | `gpt-3.5-turbo` | 对话模型名 |

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
| `CB_HISTORY_SUMMARY_MAX_TOKENS` | `2000` | 历史摘要预算 |
| `CB_RAG_MAX_TOKENS` | `1800` | RAG 段预算 |
| `CB_MEMORY_MAX_TOKENS` | `450` | memory 段预算 |
| `CB_RAG_SENT_PER_CHUNK` | `2` | 每块保留句数 |
| `CB_RAG_SENT_MAX_CHARS` | `120` | 句级压缩单句最大长度 |
| `CB_MEMORY_TOPK` | `2` | memory 注入条数 |
| `CB_MEMORY_ITEM_MAX_CHARS` | `100` | 单条 memory 注入长度 |

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

实现细节：
- 向量库为 `FAISS IndexFlatL2`（当前未采用 IVF/PQ/HNSW）。

---

## 6. Embedding 与索引构建

| 参数 | 默认值 | 作用 |
|---|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-base-zh-v1.5` | 嵌入模型 |
| `EMBEDDING_DEVICE` | `auto` | `cpu/cuda/auto` |
| `EMBEDDING_BATCH_SIZE` | 按设备自适应 | 嵌入批大小 |
| `DATA_DIR` | `./data/workspaces` | 课程工作区根目录 |

---

## 7. MCP 工具系统

| 参数 | 默认值 | 作用 |
|---|---|---|
| `MCP_PYTHON_BIN` | 当前解释器 | MCP 子进程 Python |
| `SERPAPI_API_KEY` | 无 | `websearch` 工具密钥 |

关键机制：
- 工具链路固定：`OpenAI tool_call -> ToolHub -> MCPTools.call_tool -> stdio MCP -> server_stdio`。
- MCP 客户端超时默认 `20s`，失败自动重启重试一次。
- 不做本地直调 fallback（协议一致性优先）。
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

数据模型：
- `episodes`：`qa/qa_summary/mistake/practice/exam`
- `user_profiles`：`weak_points/concept_mastery/avg_score/...`
- 记忆元数据支持 `mode/agent/phase` 过滤。

---

## 9. Router 与 Replan

| 参数 | 默认值 | 作用 |
|---|---|---|
| `ENABLE_ROUTER_REPLAN` | `1` | 是否启用重规划 |
| `REPLAN_MIN_CHARS` | `160` | 低质量判定阈值（字符） |
| `REPLAN_MIN_SENTENCES` | `2` | 低质量判定阈值（句数） |

补充：
- Replan 最多发生一次。
- 只允许发生在任何 `persist_*` 副作用步骤之前。

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

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--cases` | `benchmarks/cases_v1.jsonl` | 用例集 |
| `--gold` | `benchmarks/rag_gold_v1.jsonl` | RAG gold |
| `--output-dir` | `data/perf_runs/baseline_v1` | 输出目录 |
| `--profile` | `baseline_v1` | 评测 profile |
| `--repeats` | `2` | 每条重复次数 |

支持：
- checkpoint 断点续跑（按 `case_id#repeat` 去重）。
- 输出 `raw/summary/md/checkpoint`。
- v3 指标补充：`fallback_rate`、`resolved_mode_override_count`、`taskgraph_route`、`session_store_hit_rate`。

---

## 12. 当前本地 `.env`（脱敏快照）

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

## 13. 面试建议：最常被问的“为什么这么配”

- `ACT_MAX_TOKENS=160`：限制工具轮冗余长文本，减少中间轮耗时。
- `ALWAYS_FINAL_STREAM=1`：保证最终答案轮有可感知的 TTFT 与流式体验。
- `RAG_TOPK` 分模式：learn/practice 更偏精炼，exam 保留更高证据覆盖。
- `MEMORY_SEARCH_IN_ACT_DEFAULT=0`：避免工具轮重复检索造成 token/时延膨胀。
- `MEMORY_SEARCH_BACKEND=fts5`：在规模增长时优于纯 LIKE；仍保留 LIKE 兼容回退。
- `chapter_hybrid`：利用教材结构提升检索语义密度，同时保留 fixed 回退稳态。
