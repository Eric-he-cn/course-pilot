# 接手临时记录任务后的执行报告（2026-04-12）

## 1. 任务背景与目标

本报告说明我在接手 [temporary-record-2026-04-12.txt](temporary-record-2026-04-12.txt) 任务后，围绕 v3 升级收官所做的工作、具体实现方式、验证过程与当前结论。

任务来源核心要求：

1. 在现有 v3 架构升级基础上完成收官
2. 完成动态测评并给出可解释结果
3. 修复评测与实现中的关键 bug
4. 认真整理项目文档，保证结构清晰、关键环节不遗漏

---

## 2. 接手后执行策略

我采用了“先证据、后改动、再验证、最后文档封板”的流程：

1. 先审阅历史记录与当前仓库状态
- 读取 [temporary-record-2026-04-12.txt](temporary-record-2026-04-12.txt)
- 核对近期提交、分支与基线 tag

2. 复核 v3 计划与当前实现一致性
- 计划文档： [docs/reviews/coursepilot-architecture-upgrade-plan-2026-04-10.md](docs/reviews/coursepilot-architecture-upgrade-plan-2026-04-10.md)
- 已有进展报告： [docs/reviews/coursepilot-v3-update-report-2026-04-12.md](docs/reviews/coursepilot-v3-update-report-2026-04-12.md)

3. 定位阻塞收官的真实问题
- full30 检索命中全 0 的评测口径问题
- review 对 baseline judge（v2 无 judge）的误导性数值语义

4. 代码修复 + 产物重算 + 回归测试

5. 文档升级与最终复审归档

---

## 3. 具体工作与实现细节

## 3.1 基线与阶段状态核对

已确认关键时间线：

- 基线快照：6b7efdb
- v3 关键里程碑提交：
  - 1014d94
  - 9c96e96
  - 6977470
  - 85997de
  - 1a9b662
  - 55a35e8
  - e033341

说明：本次工作不是重启改造，而是在既有 v3 主链上完成收官修复与复审。

---

## 3.2 动态评测核心 bug 修复：RAG 命中全 0

### 问题现象

历史 full30 产物出现：
- `hit_at_k = 0.0`
- `top1_acc = 0.0`
- `precision_at_k = 0.0`

但实际引用内容并非全失败，说明评测口径存在问题。

### 根因

旧版评测逻辑仅支持：
- `case_id -> gold_doc_ids` 的单一文档级精确匹配

缺陷：
1. 缺少 cases 与 gold 的覆盖率防呆
2. 不支持页码/chunk/关键词等更稳健判定
3. 一旦 case_id 不匹配，会静默产出全 0 指标

### 修复实现

文件： [scripts/perf/bench_runner.py](scripts/perf/bench_runner.py)

新增能力：

1. gold 覆盖率校验
- 参数：
  - `--gold-min-coverage`
  - `--gold-mismatch-policy warn|fail`
- 默认覆盖率不足直接 fail，阻断误导性统计

2. 多策略命中判定
- 按优先级：
  1) `chunk_id`
  2) `doc_id + page`
  3) `doc_id`
  4) `page`
  5) `keyword`
- 新增字段：
  - `rag_match_strategy`
  - `rag_match_signal`

3. 离线重算模式
- 参数： `--recompute-only`
- 只重算已有 `baseline_raw.jsonl` 中的 RAG 指标，不重跑模型调用
- 目的：低成本修复历史产物，提升收官效率

### 验证结果

重算后 full30 指标恢复：

- `hit_at_k = 1.0`
- `top1_acc = 1.0`
- `precision_at_k = 1.0`
- `gold_case_coverage = 1.0`

结果文件： [data/perf_runs/round2_full30/baseline_summary.json](data/perf_runs/round2_full30/baseline_summary.json)

---

## 3.3 动态 review 语义修复：v2 baseline 无 judge

### 背景约束

你明确指出：v2 baseline 当时没有 LLM judge，不需要补。

### 问题

旧 review 逻辑把 baseline judge 缺失等同于 `0.0`，会造成误导。

### 修复实现

文件： [scripts/eval/review_runner.py](scripts/eval/review_runner.py)

改动：

1. 新增 judge 有效性判定
- 判断 summary 和 raw 中是否存在有效 judged 样本

2. baseline 无 judge 时输出空值
- `baseline_avg_judge_score = null`
- `delta_avg_judge_score = null`

3. Markdown 报告中显示 `N/A`
- 避免把“缺失”解释为“0分”

### 结果

产物已符合预期语义：
- [data/perf_runs/round2_full30_review/review_summary.json](data/perf_runs/round2_full30_review/review_summary.json)
- [data/perf_runs/round2_full30_review/review_summary.md](data/perf_runs/round2_full30_review/review_summary.md)

---

## 3.4 judge 可用性修复

### 问题

直接运行 judge 脚本时，可能不继承 .env，出现 `missing_judge_config`。

### 修复实现

文件： [scripts/eval/judge_runner.py](scripts/eval/judge_runner.py)

改动：
- 启动时自动加载项目根 .env
- 保留并标记 heuristic fallback 计数

### 结果

full30 judge 已真实执行：
- `num_judged = 30`
- `judge_skipped = false`

产物： [data/perf_runs/round2_full30_judge/judge_summary.json](data/perf_runs/round2_full30_judge/judge_summary.json)

---

## 3.5 测试补强

新增测试文件： [tests/test_bench_rag_eval.py](tests/test_bench_rag_eval.py)

覆盖场景：
1. doc+page 匹配
2. keyword 匹配
3. gold 覆盖率统计
4. recompute-only 对 row 级字段重写

本轮执行测试：
- `python -m unittest tests.test_bench_rag_eval tests.test_contract_fixes`
- 结果：通过（34 tests OK）

---

## 3.6 文档整理与结构化完善

## 新增文档

1. 动态测评手册： [docs/guides/evaluation.md](docs/guides/evaluation.md)
- 数据集格式
- 命中判定策略
- 防呆机制
- 命令模板
- 结果解读
- 收官清单

2. 收官复审报告： [docs/reviews/coursepilot-v3-closeout-review-2026-04-12.md](docs/reviews/coursepilot-v3-closeout-review-2026-04-12.md)

## 更新文档

1. README： [README.md](README.md)
- 增补 v3 动态测评收官流程与命令
- 明确 v2 baseline judge 缺失为 N/A 语义

2. 配置总览： [docs/guides/config-overview.md](docs/guides/config-overview.md)
- 增加 bench_runner 新参数
- 增加 RAG 命中策略与覆盖率字段

3. 贡献指南： [docs/guides/contributing.md](docs/guides/contributing.md)
- 增加评测相关测试命令
- 增加 evaluation 文档入口

---

## 4. 产物级结果汇总

## 4.1 候选 full30（重算后）

文件： [data/perf_runs/round2_full30/baseline_summary.json](data/perf_runs/round2_full30/baseline_summary.json)

关键值：
- `error_rate = 0.0`
- `fallback_rate = 0.0`
- `trace_contract_error_rate = 0.0`
- `taskgraph_step_status_coverage = 1.0`
- `hit_at_k/top1_acc/precision_at_k = 1.0`
- `gold_case_coverage = 1.0`

## 4.2 候选 judge

文件： [data/perf_runs/round2_full30_judge/judge_summary.json](data/perf_runs/round2_full30_judge/judge_summary.json)

关键值：
- `num_judged = 30`
- `num_fallback_judged = 0`
- `avg_overall_score = 0.7013`

## 4.3 动态 review

文件： [data/perf_runs/round2_full30_review/review_summary.json](data/perf_runs/round2_full30_review/review_summary.json)

关键值：
- `regression_case_count = 11`
- `human_review_queue_count = 20`
- `rag_gold_missing_case_count = 0`
- `baseline_avg_judge_score = null`

---

## 5. 收官判断

本轮任务目标达成情况：

1. 已完成：收官期关键 bug 修复
- RAG 全 0 口径问题
- baseline judge 语义问题
- judge 环境加载问题

2. 已完成：动态测评链路闭环
- bench -> judge -> review 全链路可复现
- 支持离线重算与防呆

3. 已完成：文档结构化补齐
- 新增 evaluation 专门手册
- README/config/contributing 同步更新

4. 仍需持续优化（下一轮）
- 回归队列仍有 11 条（主要为时延回归和部分低分 case）

结论：
- v3 已达到“可封板并进入专项优化”的状态。

---

## 6. 本次修改清单（按文件）

代码：
- [scripts/perf/bench_runner.py](scripts/perf/bench_runner.py)
- [scripts/eval/review_runner.py](scripts/eval/review_runner.py)
- [scripts/eval/judge_runner.py](scripts/eval/judge_runner.py)
- [tests/test_bench_rag_eval.py](tests/test_bench_rag_eval.py)

文档：
- [README.md](README.md)
- [docs/guides/config-overview.md](docs/guides/config-overview.md)
- [docs/guides/contributing.md](docs/guides/contributing.md)
- [docs/guides/evaluation.md](docs/guides/evaluation.md)
- [docs/reviews/coursepilot-v3-closeout-review-2026-04-12.md](docs/reviews/coursepilot-v3-closeout-review-2026-04-12.md)
- [docs/reviews/task-report-after-temporary-record-2026-04-12.md](docs/reviews/task-report-after-temporary-record-2026-04-12.md)
