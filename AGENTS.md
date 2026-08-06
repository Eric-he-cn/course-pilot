# 给编码 Agent 的说明

这份文件讲的是**把项目跑起来**：装依赖、配模型、启动、验证。用户说「帮我安装这个项目」时
照下面做即可，不用反复问。

**要改这个项目的代码，先读 [Docs/development.md](Docs/development.md)。** 那里有当前的任务清单、
开发工作流、两个会话并行开发的约定，以及一份踩过的坑——里面每一条都是真出过问题的，
不看会重犯。两份文件受众不同：这份给「想用」的人，那份给「想改」的人。

## 安装

前置：Python 3.11+、Node 18+、pnpm。缺哪个先装哪个（macOS 用 Homebrew，Linux 用系统包管理器）。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend && pnpm install && cd ..
cp .env.example .env
```

`sentence-transformers` 会拖进 PyTorch，首次装几百 MB、要几分钟，这是正常的。装不上也能跑，
检索会自动退回纯关键词匹配，`/api/v2/health` 会如实报告。

后端依赖就是 `backend/requirements.txt` 那几行，不用额外装东西。接外部 MCP server 没有引
第三方 SDK，JSON-RPC 是自己按协议发的（`backend/adapters/mcp_http.py`），用的是已有的 httpx。

## 配置模型

`.env` 里这五项决定能不能真的调模型：

```
TEXT_PROVIDER=            # 只是显示用的名字，随便填，比如 openai / my-llm
TEXT_API_KEY=             # 用户自己的 key
TEXT_BASE_URL=            # 填到 /chat/completions（或 /responses）之前那一段
TEXT_MODEL=               # 模型 id
COURSEPILOT_ENABLE_REMOTE_LLM=1
TEXT_PROTOCOL=            # chat（默认，打 /chat/completions）| responses（打 /responses）
TEXT_SERVER_SEARCH=0      # 厂商端联网搜索，默认关；只有 responses 协议有，见架构 §5.10
```

**不要替用户选服务商，也不要猜他的 key。** 任何说 OpenAI Chat Completions 或 Responses
协议的服务都能接，包括自建的。两条协议语义等价，选哪条看服务支持哪条；只提供 Responses 的
（如 DeepSeek 的 Responses 端点、后续 Responses-only 服务）把 `TEXT_PROTOCOL` 填成 responses。
要求：支持流式（`stream: true`）和 function calling，否则工具循环跑不起来。

四项没配齐或开关是 0 时，服务照样能启动，回答由本地兜底 responder 生成并明确标注——
这不是故障，是刻意的默认，避免误耗用户额度。

厂商私有的请求字段走 `TEXT_EXTRA_BODY`，一个 JSON 对象，原样并进请求体：

```
TEXT_EXTRA_BODY={"thinking":{"type":"disabled"}}
```

想在界面上切换多个模型，按 `TEXT_MODEL_2`、`_3`… 往下加，序号不许跳（扫到断号为止）。
同一家的第二个模型只填 `TEXT_MODEL_n` 一行，`BASE_URL` / `API_KEY` / `EXTRA_BODY` 继承第一个。

可选的两组：`VISION_*` 配好才支持图片提问与扫描版 PDF 转文字（同样要 OpenAI 兼容，
`VISION_CHAT_MODEL` 留空则复用 `VISION_MODEL`）；`RESEARCH_SERPAPI_API_KEY` 配好才会把
联网工具下发给模型。

## 其余配置项

`.env.example` 里每一项都带注释，按需要改就行。下面几组容易漏。

**上下文预算。** 换模型只改 `AGENT_MODEL_CONTEXT_WINDOW` 这一个数：软窗口默认取它的一半，
六个分区（系统提示、当前提问、会话历史、记忆与知识页、检索证据、skill 正文）的配额再按固定
比例从软窗口切出来，一起跟着缩。要覆盖推导值就填 `AGENT_CONTEXT_TOKEN_LIMIT` 与
`AGENT_HISTORY_TOKEN_BUDGET`，两者都不会超过上一层。分区之间的比例是代码常量
（`backend/core/settings.py` 的 `CONTEXT_PARTITION_RATIOS`），没有对应的环境变量。

**MCP。** 接哪台 server 由用户在界面「管理与设置」里填，地址、凭据与工具快照存在这个用户的
数据目录里，不进 `.env`。`.env` 只管三件事，而且这三项 `.env.example` 里没有列，要用自己加：

```
MCP_ALLOW_LOOPBACK=0            # 默认拒绝指向本机与内网的地址；本机自己跑着一台 server 才填 1
MCP_CONNECT_TIMEOUT_SECONDS=10
MCP_TIMEOUT_SECONDS=30
```

`MCP_ALLOW_LOOPBACK=1` 只放开回环（`127.0.0.0/8`、`::1`）；私网地址与 `169.254.169.254`
这类元数据端点无论开关如何都拒绝。

**云端检索。** 跑不动本地 BGE 的机器可以把 `RAG_EMBEDDING_MODEL` / `RAG_RERANKER_MODEL`
写成 `cloud:模型名`，再配 `RAG_CLOUD_*` 三项。代价是每次检索多一个网络往返，
而且教材内容会发到那家服务商。

`delegate`（模型把成规模的调研派成子任务）没有环境变量，每轮额度写在
`backend/modules/agent/tools.py` 的 `MAIN.per_tool_budget` 里。

## 启动

```bash
./scripts/dev.sh
```

前端 `http://127.0.0.1:5173`，后端 `http://127.0.0.1:8000/api/v2/health`。
8000 或 5173 被占用时脚本会直接失败并给出 kill 命令——这是故意的，残留的旧进程会静默接管请求。
要在同一台机器上再起一套就用 `CP_PORT_OFFSET=10 ./scripts/dev.sh`（8010 / 5183），
**同时换 `STORAGE_DATA_DIR`**，否则两套服务写同一个 SQLite。

**不要用 `python -m uvicorn` 之类的命令自己起后端再起前端**，`dev.sh` 已经处理了端口检查、
reload 目录和退出时清理子进程。

Claude Code 里可以直接用 `.claude/launch.json` 里的 `coursepilot-dev` 启动预览，它包的就是这个脚本。

首屏是登录页，输任意用户名即可。每个用户名对应一份独立的数据库与文件目录。
这不是身份认证，没有密码。

## 验证装好了

```bash
./scripts/check.sh
```

六道门：后端全部测试、Python 编译检查、界面文案漏走 i18n 的检查、后端产出的 i18n key 与前端
字典对账、前端类型检查、前端生产构建。全绿说明装对了。
这个命令不需要配 API key，也不发任何网络请求。

当前基线：`pytest` **784 passed / 1 skipped**——跳过那条要真的向量模型，
设 `COURSEPILOT_TEST_EMBEDDINGS=1` 才跑。另外有几条用例按真实教材的目录结构写，
`testdata/fixtures/` 里没有切片教材时它们也会跳过。

## 要真调模型的端到端

这几个脚本会真花额度，**都要另起一套实例，不要对着开发库 `data/` 跑**：端口用 `CP_PORT_OFFSET`
错开，数据目录用 `STORAGE_DATA_DIR` 分开，两个一起带。

| 脚本 | 覆盖什么 |
| --- | --- |
| `scripts/e2e_journey.py` | 从空库走完一条有状态的旅程，一轮一个能力点 |
| `scripts/e2e_multiturn.py` | 六个场景，每个把同一件事拆到几轮，后一轮依赖前一轮的产物 |
| `scripts/e2e_library.py` | 资料库那条链路：上传 → 索引 → 目录结构 → 知识页，全程一句话都不问 |
| `scripts/eval_dataset.py` | `evals/dataset.yaml` 的标注样本；`--no-judge` 只跑不花钱的那两个维度 |
| `evals/wiki_gain.py` | 知识页收益的 A/B，判据是事实锚点（`evals/wiki_anchors.yaml`） |

```bash
CP_PORT_OFFSET=1 STORAGE_DATA_DIR=testdata/e2e-fresh ./scripts/dev.sh
.venv/bin/python scripts/e2e_journey.py --base http://127.0.0.1:8001 --data-dir testdata/e2e-fresh
```

`--data-dir` 必须和实例的 `STORAGE_DATA_DIR` 是同一个：脚本会直接读那份 SQLite 做断言。
各脚本的默认端口与目录不一样，翻它自己的 docstring。

界面上的回归清单是人工过的，写在
[Docs/coursepilot-2.0-e2e-browser-test.md](Docs/coursepilot-2.0-e2e-browser-test.md)。

## 想要一份能直接玩的示例数据

```bash
./scripts/dev.sh                                   # 另开一个终端，保持运行
.venv/bin/python scripts/example_setup.py          # 一门课，下载约 120 KB
.venv/bin/python scripts/example_setup.py --all    # 四门课，首次要下约 70 MB
```

跑完用 `example` 这个用户名登录，就有带教材和索引的课程可以直接提问。
教材是脚本从各自官网下载的公开教材切片，不在仓库里。

## 改代码时注意

- 改完跑 `./scripts/check.sh`，别只跑一部分。
- 后端测试要 `PYTHONPATH=backend`，项目里没有 pytest 配置文件。
- 界面文案一律走 `frontend/src/i18n.ts`，中英两份字典都要加；后端每轮上屏的文案发的是 key
  而不是句子，加 key 别忘了加翻译。这两件事 `check.sh` 都有门在守。
- 数据库 schema 改动一律新增 migration，写在 `backend/core/store.py` 的 `MIGRATIONS` 末尾，
  不要改已有条目。增删列例外：走同一文件里的 `ADDED_COLUMNS` / `RETIRED_COLUMNS` 按现存结构对账。
  `ALTER` 不幂等，写成编号迁移一旦中途失败，版本号没落库而 DDL 已提交，工作区就再也起不来。
- 组装只在 `backend/app/bootstrap.py` 一处发生，模块自己不 new 仓储和适配器。
- 项目不含任何发布或部署操作，也不要加。
