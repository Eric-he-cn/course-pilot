# CoursePilot v3 动态测评指南

更新时间：2026-04-12

本文档面向开发与评测同学，说明 v3 动态测评链路、数据格式、命中判定策略与常见排障方式。

---

## 1. 测评目标

动态测评分三层：

1. 基线运行层：`bench_runner.py`
- 跑用例集，生成 `baseline_raw.jsonl`、`baseline_summary.json`、`baseline_summary.md`
- 统计时延、错误率、fallback、taskgraph 覆盖、RAG 命中等指标

2. 质量评分层：`judge_runner.py`
- 对候选回答做 LLM-as-judge 打分
- 输出 `judge_raw.jsonl` 与 `judge_summary.json`

3. 动态复评层：`review_runner.py`
- 合并 benchmark + judge（可含 baseline 对照）
- 输出 `review_summary.json/.md`、`regression_cases.jsonl`、`human_review_queue.jsonl`

---

## 2. 评测数据格式

### 2.1 cases 文件（示例）

每行一个 JSON：

```json
{"case_id":"learn_01","mode":"learn","course_name":"矩阵理论","message":"矩阵的秩是什么？"}
```

关键字段：
- `case_id`: 唯一主键
- `mode`: `learn/practice/exam`
- `course_name`: 工作区课程名
- `message`: 用户输入
- `history`（可选）: 历史轮次
- `need_rag` / `requires_citations`（可选）: trace contract 校验开关

### 2.2 gold 文件（支持扩展）

每行一个 JSON，`case_id` 必须与 cases 对齐。

最小格式：

```json
{"case_id":"learn_01","gold_doc_ids":["矩阵理论教案.pdf"]}
```

扩展格式（推荐）：

```json
{
  "case_id":"learn_01",
  "gold_doc_ids":["矩阵理论教案.pdf"],
  "gold_pages":[206],
  "gold_chunk_ids":["矩阵理论教案.pdf_p206_c0"],
  "gold_keywords":["矩阵秩","线性无关"],
  "should_retrieve":true
}
```

字段说明：
- `gold_doc_ids`: 文档级命中目标
- `gold_pages`: 页码级命中目标
- `gold_chunk_ids`: chunk 级命中目标
- `gold_keywords`: 关键词兜底目标
- `should_retrieve`: 若为 `false`，该 case 不计 RAG 命中

---

## 3. RAG 命中判定策略（v3）

`bench_runner.py` 采用分层策略，按优先级选择匹配方式：

1. `chunk_id`（最高优先）
- gold 提供 `gold_chunk_ids` 时使用

2. `doc_page`
- gold 同时提供 `gold_doc_ids` + `gold_pages` 时使用

3. `doc_id`
- 仅提供 `gold_doc_ids` 时使用

4. `page`
- 仅提供 `gold_pages` 时使用

5. `keyword`
- 仅提供 `gold_keywords` 时使用

输出字段：
- `rag_hit`
- `rag_top1`
- `rag_precision`
- `rag_has_gold`
- `rag_match_strategy`
- `rag_match_signal`

---

## 4. 防呆机制（避免静默全 0）

`bench_runner.py` 新增 gold 覆盖率校验：

- 指标：`gold_case_coverage = gold_matched_case_count / case_total`
- 默认阈值：`0.5`
- 默认策略：`fail`

当 case 与 gold 的 `case_id` 大面积不匹配时，评测会直接失败，不再产出误导性的 `rag_hit=0`。

可配置参数：
- `--gold-min-coverage`
- `--gold-mismatch-policy warn|fail`

---

## 5. 常用命令

### 5.0 推荐数据集分层

| 数据集 | 目标 | 说明 |
|---|---|---|
| `benchmarks/smoke_contract.jsonl` | 快速回归 | 覆盖基本路由与引用形状 |
| `benchmarks/cases_v1_top5.jsonl` | 小样本质量 | 便于 PR 级验证 |
| `benchmarks/cases_v1.jsonl` | 全量基线 | 30 cases 基线对比 |
| `benchmarks/core_e2e.jsonl` | 端到端质量 | 用于 judge/review |
| `benchmarks/multi_turn_sessions.jsonl` | 多轮与续聊 | SessionState 恢复验证 |
| `benchmarks/tooling_and_permissions.jsonl` | 工具治理 | 权限/预算/幂等验证 |
| `benchmarks/dynamic_review_queue.jsonl` | 难例回归 | 来自历史回归队列 |

### 5.1 跑 full30（首次）

```bash
python scripts/perf/bench_runner.py \
  --cases benchmarks/cases_v1.jsonl \
  --gold benchmarks/rag_gold_v1.jsonl \
  --output-dir data/perf_runs/round2_full30 \
  --profile round2_full30 \
  --repeats 1
```

### 5.2 已有 raw 离线重算 RAG 指标（不重跑模型）

```bash
python scripts/perf/bench_runner.py \
  --cases benchmarks/cases_v1.jsonl \
  --gold benchmarks/rag_gold_v1.jsonl \
  --output-dir data/perf_runs/round2_full30 \
  --profile round2_full30 \
  --recompute-only
```

### 5.3 跑候选侧 judge

```bash
python scripts/eval/judge_runner.py \
  --raw data/perf_runs/round2_full30/baseline_raw.jsonl \
  --cases benchmarks/cases_v1.jsonl \
  --baseline-raw data/perf_runs/fullfix_full30_20260326/baseline_raw.jsonl \
  --output-dir data/perf_runs/round2_full30_judge
```

说明：v2 基线没有 LLM judge 是允许的，review 会将 baseline judge 记为 `N/A`。

### 5.4 生成动态复评报告

```bash
python scripts/eval/review_runner.py \
  --benchmark-summary data/perf_runs/round2_full30/baseline_summary.json \
  --benchmark-raw data/perf_runs/round2_full30/baseline_raw.jsonl \
  --judge-summary data/perf_runs/round2_full30_judge/judge_summary.json \
  --judge-raw data/perf_runs/round2_full30_judge/judge_raw.jsonl \
  --baseline-benchmark-summary data/perf_runs/fullfix_full30_20260326/baseline_summary.json \
  --baseline-benchmark-raw data/perf_runs/fullfix_full30_20260326/baseline_raw.jsonl \
  --output-dir data/perf_runs/round2_full30_review
```

### 5.5 在线影子评测（异步，不阻塞主链路）

1. 前端开启 `🧪 开启影子评测`（会话级）
2. 正常对话时，后端会把样本写入：
   - `data/perf_runs/online_eval/<date>/eval_queue.jsonl`
3. 后台 worker 自动消费并产出：
   - `benchmark_raw_online.jsonl`
   - `benchmark_summary_online.json`
   - `judge/judge_summary.json`
   - `review/review_summary.json`

可控开关（环境变量）：
- `ONLINE_EVAL_WORKER_ENABLED`
- `ONLINE_EVAL_POLL_SEC`
- `ONLINE_EVAL_RUN_JUDGE_REVIEW`
- `ONLINE_EVAL_PYTHON_BIN`

---

## 6. 结果解读建议

强约束 gate：
- `error_rate == 0`
- `fallback_rate == 0`
- `trace_contract_error_rate == 0`
- `taskgraph_step_status_coverage == 1.0`

RAG gate：
- 必须先看 `gold_case_coverage`
- 覆盖不达标时，`hit_at_k/top1/precision` 不可用于质量结论

review gate：
- 先看 `regression_case_count`
- 再看 `human_review_queue_count`
- `rag_gold_missing_case_count > 0` 优先处理数据对齐问题

---

## 7. 常见问题

1. `hit_at_k` 突然全 0，但引用看起来正常
- 先检查 `gold_case_coverage`
- 常见原因是 `cases` 与 `gold` 文件不配套

2. `judge_skipped=true`
- 检查 `.env` 或环境变量中的 `OPENAI_API_KEY` / `EVAL_JUDGE_API_KEY`
- `judge_runner.py` 已支持自动加载项目根目录 `.env`

3. baseline judge 为空
- v2 历史 baseline 无 judge 是正常现象
- review 中 baseline judge 会显示 `N/A`

---

## 8. 收官清单

1. 跑完 smoke/top5/full30/strict smoke
2. full30 通过 gold 覆盖校验（非 0 且达到阈值）
3. judge 运行成功（或明确标注 fallback）
4. review 生成并输出回归队列
5. 文档更新：README + config-overview + 本文档
