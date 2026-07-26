# CoursePilot 2.0 Demo

个人开源学习助手的本地端到端 Demo。当前包含通用/课程会话、每轮课程解析（支持沿用近期解析结果）、带多轮历史与工具循环的流式 Agent 对话（每轮先用用户问题自动做一次课程证据检索作为种子，模型再按需自主调用检索/资料清单/计划/档案工具，带步数上限；引用跨工具去重编号）、课程隔离的混合检索（BGE 语义向量 + 词面，RRF 融合，支持中文问题命中英文教材）、带页码引用的 RAG 资料库、每轮对话落盘的 JSONL trace、可版本化读写的学习计划（写入需用户在对话里明确要求，历史条目不可改写）、按白名单收窄权限的用户 Skill 导入、可选 Wiki 与学习档案，以及在流式过程中展示“查了什么”和本轮上下文构成的 React 前端。

语义检索使用 `RAG_EMBEDDING_MODEL`（默认 `BAAI/bge-base-zh-v1.5`）；`sentence-transformers` 缺失或模型加载失败时自动退回纯词面检索，health 会如实报告。在向量能力加入之前上传的教材需要在知识仓库点一次“重建索引”才有语义召回。跨语言检索质量受模型限制，换用 `BAAI/bge-m3` 并重建索引可获得更好的中英互检效果。

## 本地启动

要求 Python 3.11+ 与 pnpm。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend && pnpm install && cd ..
./scripts/dev.sh
```

浏览器打开 `http://127.0.0.1:5173`。后端健康检查位于 `http://127.0.0.1:8000/api/v2/health`。

真实 DeepSeek Adapter 已接入：当 `.env` 同时配置 `TEXT_API_KEY`、`TEXT_MODEL=deepseek-v4-flash` 和 `COURSEPILOT_ENABLE_REMOTE_LLM=1` 时，课程解析和 RAG 命中后以流式方式调用 DeepSeek，逐段下发 `text_delta`。未启用或在输出任何增量前失败时，回退到有明确标识的本地 responder；已输出增量后中断则保留部分回答并发出 `stream_interrupted`，不静默重放。仓库示例配置仍默认关闭远端调用，避免误耗额度。教材支持 PDF/TXT/MD（PDF 引用带页码），默认上限 100 MiB；图片附件上限仍是 10 MiB。

## 目录约定

`data/` 只放真实使用产生的数据，测试与验证产物一律不进来：

```
data/                         用户工作区（STORAGE_DATA_DIR 默认指向这里）
├─ coursepilot.db             会话、证据事件、掌握度、计划、产物
├─ user.md                    跨课程画像（可直接编辑）
├─ courses/<课程 id>/memory.md  该课程的情景记忆（可直接编辑）
├─ materials/<课程 id>/        上传的教材原件
├─ notes/<课程 id>/            助手写的课程笔记与学习卡片（markdown）
├─ wiki/<课程 id>/             可选的 Course Wiki
├─ traces/                    每轮对话的 JSONL + payloads/
└─ backups/                   手工备份

testdata/                     测试与验证，不是用户数据
├─ fixtures/                  开源教材切片（scripts/e2e_fixture.py 下载）
└─ e2e/                       端到端测试实例的独立数据目录
```

两个目录都在 `.gitignore` 里。想连测试实例就 `STORAGE_DATA_DIR=testdata/e2e`。

## 验证

```bash
./scripts/check.sh
```

该命令运行完整后端测试、Python 编译检查、前端类型检查与生产构建。项目不包含任何发布或部署操作。

后端运行时可另开终端执行真实 HTTP smoke；该命令在远端开关启用时会产生一次 DeepSeek 调用：

```bash
.venv/bin/python scripts/smoke.py
```

浏览器端到端测试见 [端到端测试清单](Docs/coursepilot-2.0-e2e-browser-test.md)：教材取自开源教材的章节切片，
用 `scripts/e2e_fixture.py` 准备，测试实例跑在独立数据目录 `testdata/e2e`，不影响开发库。

## 文档

| 文档 | 用途 |
| --- | --- |
| [项目介绍](Docs/项目介绍.md) | 总览：背景、能做什么、各模块的设计思路 |
| [产品设计](Docs/coursepilot-2.0.md) | 定位、功能模块、分期规划、非目标 |
| [技术架构](Docs/coursepilot-2.0-architecture.md) | 模块边界、LLM 接入、Skill 体系、存储、掌握度、评测分层 |
| [前端设计](Docs/coursepilot-2.0-frontend-design.md) | 视觉方案、信息架构、核心页面、组件与状态 |
| [开发中](Docs/开发中.md) | 当前进度、任务优先级、开发工作流、踩过的坑 |
| [端到端测试](Docs/coursepilot-2.0-e2e-browser-test.md) | 浏览器回归清单 |

## 评测

三层，都需要一个已准备好教材与索引的实例在跑（远端模型开启）：

```bash
.venv/bin/python scripts/benchmark.py                     # 冒烟：固定用例跑真实链路，只断言结构化行为
.venv/bin/python scripts/evaluate.py --data-dir testdata/e2e  # 抽样：judge 给忠实度/归因/有用性打分
.venv/bin/python scripts/replay_mastery.py --data-dir testdata/e2e --save baseline.json
```

`e2e_journey.py` 是一条有状态的连贯旅程：从空库开始建课、索引教材、提问取证、练习闭环、
排计划、画图存笔记、错题复盘、联网调研、课程边界、会话管理，最后核对 trace，共 30 项断言。
与 benchmark 的区别是后一步依赖前一步的产物。用法：

```bash
STORAGE_DATA_DIR=testdata/e2e-fresh .venv/bin/python -m uvicorn app.main:app --app-dir backend --port 8001
.venv/bin/python scripts/e2e_journey.py --base http://127.0.0.1:8001 --data-dir testdata/e2e-fresh
```

`benchmark.py` 覆盖 practice 的出题、单题作答、多题作答、讲评、变式题、作答对象歧义，
外加取证引用、课程隔离与课程解析；断言的是 SSE 事件与档案增量，模型换措辞不会假失败。
`evaluate.py` 的评分按 `prompt_version` 聚合，改提示词后新旧版本可分别对比。
`replay_mastery.py` 在改掌握度算法前存基线、改完对比；事件数变化的概念会被标为"数据已变化，
不可比"，只有相同事件流下的差异才算算法影响。跑 benchmark 或评测时不要同时改后端代码——
dev.sh 的 `--reload` 会重启进程并切断正在进行的 SSE。
