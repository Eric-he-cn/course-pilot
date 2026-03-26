# CoursePilot 进展报告（Full Fix 分支）

更新时间：2026-03-26  
分支：`feature/full-fix-opt-20260326`  
对比主干：`main`（`1223085`）→ 当前分支（`9bfc52b` + 未提交热修）

---

## 1. 本次范围与目标

本轮按“正确性优先、再一致性、再工程化增强”的顺序，完成了任务 1-9：

1. 修复 `/chat` 同步链路生成器陷阱。  
2. 统一 exam 联网策略（默认允许、受工具契约约束）。  
3. 依赖版本真源统一（`requirements.txt` 主导）。  
4. 上下文分区化（`history/rag/memory/final`）。  
5. Runner 与 QuizMaster 记忆检索去重统一（request 级复用）。  
6. RAG 压缩责任收敛（`retriever` vs `budgeter`）。  
7. Structured Outputs 灰度接入（Quiz/Exam/Grader）。  
8. 记忆检索 FTS5 双路径（`fts5` 优先，`like` 回退）。  
9. MCP 进度可观测性增强（`tool_progress` + 更完整状态流）。

另外补了一个 UI 热修：上下文预算角标长期显示 `0%` 的问题（后端补事件 + 前端兜底计算）。

---

## 2. 变更概览（代码层）

分支相对 `main` 的变更统计：

- 13 个核心文件变更
- `993` 行新增，`498` 行删除
- 覆盖：编排层、Agent 层、LLM 兼容层、记忆层、MCP 客户端、前端与文档

关键文件：

- `core/orchestration/runner.py`
- `core/orchestration/context_budgeter.py`
- `core/agents/quizmaster.py`
- `core/agents/grader.py`
- `core/agents/tutor.py`
- `core/orchestration/prompts.py`
- `core/llm/openai_compat.py`
- `memory/store.py`
- `mcp_tools/client.py`
- `frontend/streamlit_app.py`

---

## 3. 审阅结论（代码/行为）

### 3.1 总体结论

- 未发现阻塞合并的 P0 正确性问题（核心链路可跑通）。
- 主要收益落在：编排稳定性、可观测性、结构化输出稳健性、E2E 时延。
- 仍有 2 个需要合并后继续跟踪的风险点（见 3.2）。

### 3.2 发现与风险（按优先级）

1. `Medium`：检索耗时指标回升明显（尤其 `learn` 模式）。
   - 现象：`fullfix_full30_20260326` 的 `avg/p95_retrieval_ms` 相比上一版显著上升。
   - 影响：虽然 E2E 仍下降，但检索阶段余量变小，后续扩容和高并发风险增加。
   - 建议：单独做 retrieval profiling（dense、bm25、fusion、format 各段分解）并加 warmup 基线。

2. `Medium`：`pytest -q` 在仓库根目录会误扫受限目录（`pytest-cache-files-*`）导致收集报错。
   - 现象：`PermissionError` during collection（非业务代码失败）。
   - 影响：CI/本地一键回归不稳定。
   - 建议：统一用 `pytest tests -q`，并在根目录补 `pytest.ini` 的 `testpaths=tests`。

3. `Low`：Practice/Exam 仍可见少量结构化解析失败日志（已自动回退，不影响完成率）。
   - 影响：质量波动风险仍在，但主链路可用。
   - 建议：继续收紧 schema 与提示词，增加失败样本集回归。

---

## 4. 验证结果

执行结果：

1. `python -m py_compile`（关键改动文件）通过。  
2. `pytest tests -q` 通过：`10 passed`。  
3. 性能回归：
   - smoke9：`data/perf_runs/fullfix_smoke9_20260326/`
   - full30：`data/perf_runs/fullfix_full30_20260326/`（30/30，`done=true`）

---

## 5. 性能对比

## 5.1 对比 A：`baseline_v2_real_r1` → `fullfix_full30_20260326`

| 指标 | baseline_v2_real_r1 | fullfix_full30_20260326 | 变化 |
|---|---:|---:|---:|
| avg_prompt_tokens | 4957.3 | 2524.6 | -49.1% |
| avg_llm_ms | 31698.0 | 19176.8 | -39.5% |
| p50_first_token_latency_ms | 60535.3 | 42957.9 | -29.0% |
| p50_e2e_latency_ms | 107830.4 | 46714.3 | -56.7% |
| p95_e2e_latency_ms | 155666.4 | 106810.4 | -31.4% |
| avg_retrieval_ms | 82.3 | 88.1 | +7.0% |
| p95_retrieval_ms | 100.1 | 275.8 | +175.7% |
| tool_success_rate | 95.7% | 100.0% | +4.3pp |
| error_rate | 0.0% | 0.0% | 持平 |

模式拆分（vs baseline_v2）：

- `learn`：prompt / llm / e2e 明显下降；retrieval 回升明显。  
- `practice`：prompt、llm、e2e 全部改善，retrieval 下降（更快）。  
- `exam`：prompt、llm、e2e 全部改善，retrieval 下降（更快）。

## 5.2 对比 B：`latency_opt_full30_v2_stream` → `fullfix_full30_20260326`

| 指标 | latency_opt_full30_v2_stream | fullfix_full30_20260326 | 变化 |
|---|---:|---:|---:|
| avg_prompt_tokens | 2563.5 | 2524.6 | -1.5% |
| avg_llm_ms | 38544.3 | 19176.8 | -50.2% |
| p50_first_token_latency_ms | 7516.8 | 42957.9 | +471.5% |
| p50_e2e_latency_ms | 62611.6 | 46714.3 | -25.4% |
| p95_e2e_latency_ms | 140061.3 | 106810.4 | -23.7% |
| avg_retrieval_ms | 18.4 | 88.1 | +378.0% |

说明：

- E2E 与 LLM 总耗时有显著收益。  
- 但 `p50_first_token_latency_ms` 和 retrieval 指标相对上个版本变差，需要专项优化。  
- 这和“最终回答轮单独流式 + 中间 Act 轮收敛”策略有关：体验上依赖状态事件保活，纯 token 首字延迟会上移。

---

## 6. 文档一致性修正（本轮已同步）

已更新：

1. README：补充新元事件、新增配置项口径、链接本报告。  
2. ARCHITECTURE：补充上下文分区、工具契约、Structured Outputs、FTS5 双路径。  
3. USAGE：修正检索默认参数口径，补充上下文预算角标说明。  

---

## 7. 合并前建议清单

1. 你本地再做一次前后端联调（learn/practice/exam 各 1 条）。  
2. 重点观察两件事：
   - 上下文预算角标是否随请求变化（不再固定 0%）
   - Practice/Exam 的结构化出题和评分是否稳定
3. 如果联调通过，再合并到 `main`（建议保留本报告与评测产物路径引用）。

---

## 8. 合并后第一优先级（建议）

1. 做 retrieval profiling 并定位回升来源（分段耗时 + warmup）。  
2. 增加 `pytest.ini` 固化 `testpaths=tests`，消除根目录误扫。  
3. 对 Structured Outputs 失败样本做专项回归集（Practice/Exam 各 20 条）。

