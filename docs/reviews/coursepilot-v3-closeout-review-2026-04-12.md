# CoursePilot v3 收官复审报告（2026-04-12）

## 1. 复审范围

本轮复审覆盖三类目标：

1. v3 改进计划收官状态
2. v3 动态测评运行与结果分析
3. 文档完整性与一致性

复审对象：
- 代码：`core/`、`scripts/perf/`、`scripts/eval/`
- 测评产物：`data/perf_runs/round2_*`
- 文档：`README.md`、`docs/guides/*`、`docs/reviews/*`

---

## 2. 本轮已完成修复

### 2.1 RAG 命中全 0 问题（已修）

问题现象：
- `round2_full30` 之前出现 `hit_at_k/top1/precision` 全 0。

根因：
- 评测脚本早期仅支持 `case_id -> gold_doc_ids` 的精确匹配；
- cases 与 gold 不配套时缺少强制防呆，导致静默产出全 0。

修复内容：
- `bench_runner.py` 增加 gold 覆盖率校验（阈值 + fail/warn 策略）
- 增加多策略命中判定：`chunk_id/doc_page/doc_id/page/keyword`
- 增加 `--recompute-only`，支持离线重算已有 raw 的 RAG 指标

结果：
- `data/perf_runs/round2_full30/baseline_summary.json` 已更新为 `hit_at_k/top1/precision = 1.0`
- `gold_case_coverage = 1.0`

### 2.2 review 基线 judge 口径（已修）

背景：
- v2 baseline 历史上没有 LLM judge。

修复内容：
- `review_runner.py` 对 baseline judge 缺失场景输出 `N/A`（JSON 为 `null`），不再误导为 `0.0`

结果：
- `data/perf_runs/round2_full30_review/review_summary.json`
  - `baseline_avg_judge_score = null`
  - `delta_avg_judge_score = null`

### 2.3 judge 配置可用性（已修）

修复内容：
- `judge_runner.py` 启动时自动加载项目根目录 `.env`
- 保留 heuristic fallback，并在 summary 中区分 `num_fallback_judged`

结果：
- `round2_full30_judge` 已实现 `num_judged=30, judge_skipped=false`

---

## 3. 动态测评结果（本轮复审口径）

### 3.1 候选 full30（重算后）

来源：`data/perf_runs/round2_full30/baseline_summary.json`

关键指标：
- `num_rows = 30`
- `error_rate = 0.0`
- `fallback_rate = 0.0`
- `trace_contract_error_rate = 0.0`
- `taskgraph_step_status_coverage = 1.0`
- `hit_at_k = 1.0`
- `top1_acc = 1.0`
- `precision_at_k = 1.0`
- `gold_case_coverage = 1.0`

### 3.2 候选 judge

来源：`data/perf_runs/round2_full30_judge/judge_summary.json`

关键指标：
- `num_rows = 30`
- `num_judged = 30`
- `num_fallback_judged = 0`
- `avg_overall_score = 0.7013`
- 结构分布：`pass=14, warn=12, fail=4`

### 3.3 动态 review

来源：`data/perf_runs/round2_full30_review/review_summary.json`

关键指标：
- `regression_case_count = 11`
- `human_review_queue_count = 20`
- `rag_gold_missing_case_count = 0`
- `delta_p50_e2e_latency_ms = +10604.5`
- baseline judge 为空（按设计记为 `N/A`）

解释：
- 当前主要风险不再是“数据口径错误”，而是“部分 case 的时延回归 + 部分低分 case 需要人工复核”。

---

## 4. 文档状态

本轮已补齐并更新：

1. `README.md`
- 新增 v3 动态测评收官流程（bench/judge/review + 离线重算）

2. `docs/guides/config-overview.md`
- 增加 bench_runner 新参数
- 明确 RAG 命中判定策略与 gold 覆盖率字段

3. `docs/guides/evaluation.md`（新增）
- 数据格式、命中策略、防呆机制、命令模板、结果解读、收官清单

4. `docs/guides/contributing.md`
- 补充评测相关测试命令与评测文档入口

---

## 5. 收官判断

### 5.1 结论

- v3 改进计划：**基本收官（可进入发布前封板）**
- 动态测评体系：**链路闭环已完成**
- 文档：**结构化完善，关键环节已补齐**

### 5.2 剩余关注项（非阻塞）

1. 回归队列 20 条建议按优先级继续清理（以 `e2e_latency_regressed` 与低分 case 为先）
2. 若后续需要与 v2 做 judge 维度可比，可补离线人工标注或回放评审

---

## 6. 推荐封板动作

1. 固化本轮评测产物目录（保留 `round2_full30*`）
2. 提交代码与文档变更
3. 打收官标签（例如 `v3-closeout-r1`）
4. 进入下一轮专项优化（时延与 practice 质量）
