# 贡献指南

感谢你对 CoursePilot 的关注。本指南说明如何提交改进、修复与评测贡献。

---

## 1. 提交 Issue

### Bug 报告

请包含：

- 复现步骤
- 预期与实际行为
- 环境信息（Python 版本、系统、模型与配置）
- 相关日志或最小复现样例

### 功能建议

请包含：

- 目标问题与使用场景
- 可选方案与取舍
- 对现有流程的影响

---

## 2. 开发流程

```bash
git clone https://github.com/YOUR_USERNAME/course-pilot.git
cd course-pilot
git checkout -b feature/your-feature-name
```

建议先跑一次基础测试：

```bash
py -3.11 tests/test_basic.py
py -3.11 -m unittest tests.test_contract_fixes tests.test_mcp_stdio tests.test_bench_rag_eval tests.test_v3_priority_plan
```

如果你已经激活 Python 3.11 环境，也可以把上面的 `py -3.11` 替换成 `python`。请避免直接使用系统默认的 Python 3.6/3.7 运行回归测试。

完成修改后提交：

```bash
git add .
git commit -m "feat: ..."
git push origin feature/your-feature-name
```

---

## 3. 代码规范

- 遵循 PEP 8
- 尽量添加类型注解
- 保持函数与类的单一职责
- 修改接口时同步更新文档与评测

---

## 4. 评测贡献

新增或修改评测时，请同时更新：

- `benchmarks/` 中的数据集
- `docs/guides/evaluation.md`
- 对应的 `scripts/perf/` 或 `scripts/eval/` 脚本

推荐的验证顺序：

```bash
py -3.11 scripts/eval/dataset_lint.py --path benchmarks --output-dir data/perf_runs/_lint
py -3.11 scripts/perf/bench_runner.py --cases benchmarks/smoke_contract.jsonl --gold benchmarks/rag_gold_v2.jsonl --output-dir data/perf_runs/smoke
```

---

## 5. 扩展指南

### 新增 Agent

1. 在 `core/agents/` 新建 Agent 类
2. 在 `core/orchestration/prompts.py` 增加 prompt
3. 在 `core/runtime/executor.py` 中接入执行路径（如需）
4. 更新 `docs/guides/architecture.md`

### 新增工具

1. 在 `mcp_tools/` 添加工具实现与 schema
2. 在 `core/services/tool_hub.py` 调整权限与预算
3. 增加对应测试并更新安全文档

### 新增评测集

1. 在 `benchmarks/` 添加用例与 gold
2. 运行 `dataset_lint.py` 校验格式
3. 更新 `docs/guides/evaluation.md`

---

如需更深入的架构理解，请先阅读 `docs/guides/architecture.md`。
