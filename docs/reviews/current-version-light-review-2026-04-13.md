# CoursePilot 当前版本轻量审阅（2026-04-13）

## 审阅范围

- 当前分支：`v3/architecture-upgrade`
- 本轮重点：`backend/api.py`、`core/services/shadow_eval_service.py`
- 交叉核对：`README.md`、`docs/guides/config-overview.md`、`docs/guides/contributing.md`、`frontend/streamlit_app.py`、`tests/test_v3_priority_plan.py`

---

## Findings

### 1. 配置默认值存在代码/文档漂移

- `core/services/shadow_eval_service.py` 当前将 `ONLINE_EVAL_WORKER_ENABLED` 默认值改为 `0`（默认关闭）。
- 但 `README.md` 和 `docs/guides/config-overview.md` 仍写成默认 `1`。
- 影响：开发者会误以为在线影子评测 worker 会自动启动，导致排障和资源预期偏差。
- 处理：本轮已修复文档与前端帮助文案，统一为“默认关闭，显式开启”。

### 2. `API_RELOAD` 行为已经收敛，但缺少公开说明

- `backend/api.py` 已改为通过 `API_RELOAD` 控制 uvicorn reload，不再默认强开。
- 文档此前没有同步说明该环境变量，也没有说明默认行为。
- 影响：开发/生产环境可能继续沿用“默认热重载”的旧认知。
- 处理：本轮已在 README 和配置总览中补齐说明，并明确默认值为 `0`。

### 3. 回归测试对解释器与临时目录环境较敏感

- 仓库当前环境里系统默认 `python` 仍可能落到 Python 3.6，但项目与测试实际要求 3.11。
- `tests/test_v3_priority_plan.py` 还依赖系统临时目录，容易在受限 Windows 环境下出现假红。
- 影响：本地验证结果容易被解释器版本或系统临时目录权限污染。
- 处理：本轮已把贡献文档中的回归命令明确到 Python 3.11，并将相关测试切换到仓库内临时目录；同时修正 `memory.manager` 的全局 store 复用逻辑，使 `MEMORY_DB_PATH` 覆盖更稳定。

---

## 本轮已修复

- 统一 `ONLINE_EVAL_WORKER_ENABLED` 默认值说明：
  - `README.md`
  - `docs/guides/config-overview.md`
  - `docs/guides/evaluation.md`
  - `frontend/streamlit_app.py`
- 补齐 `API_RELOAD` 的公开配置说明：
  - `README.md`
  - `docs/guides/config-overview.md`
- 收敛本地验证入口到 Python 3.11：
  - `README.md`
  - `docs/guides/contributing.md`
- 修复环境敏感测试：
  - `tests/test_v3_priority_plan.py`
  - `memory/manager.py`

---

## 未修但保留的风险

- 前端帮助文案仍声明“考试模式默认允许联网搜索”；这与当前 `ToolPolicy` 的真实行为一致，因此本轮未改，但后续如果产品策略要切到“考试禁网”，需要同步修改 UI、策略和测试。
- 当前工作区里曾出现 `git status` 对无内容 diff 文件的索引刷新噪音；本轮未把它当作代码缺陷处理。

---

## 验证结果

使用解释器：

```bash
C:\Users\10423\miniconda3\envs\study_agent\python.exe
```

执行命令：

```bash
& 'C:\Users\10423\miniconda3\envs\study_agent\python.exe' -m unittest tests.test_v3_priority_plan tests.test_contract_fixes
```

结果：

- 修复前：`tests.test_v3_priority_plan` 中 3 个用例受系统临时目录/全局 store 复用影响失败。
- 修复后：`tests.test_v3_priority_plan` 与 `tests.test_contract_fixes` 共 38 个用例全部通过。
