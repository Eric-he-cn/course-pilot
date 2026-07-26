# 给编码 Agent 的说明

这份文件是给 Claude Code、Codex 这类编码 Agent 看的。用户可能直接说「帮我安装这个项目」，
照下面的步骤做即可，不用反复问。

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

## 配置模型

`.env` 里这五项决定能不能真的调模型：

```
TEXT_PROVIDER=            # 只是显示用的名字，随便填，比如 openai / my-llm
TEXT_API_KEY=             # 用户自己的 key
TEXT_BASE_URL=            # 填到 /chat/completions 之前那一段
TEXT_MODEL=               # 模型 id
COURSEPILOT_ENABLE_REMOTE_LLM=1
```

**不要替用户选服务商，也不要猜他的 key。** 任何兼容 OpenAI Chat Completions 的服务都能接，
包括自建的。要求：支持流式（`stream: true`）和 function calling，否则工具循环跑不起来。

四项没配齐或开关是 0 时，服务照样能启动，回答由本地兜底 responder 生成并明确标注——
这不是故障，是刻意的默认，避免误耗用户额度。

厂商私有的请求字段走 `TEXT_EXTRA_BODY`，一个 JSON 对象，原样并进请求体：

```
TEXT_EXTRA_BODY={"thinking":{"type":"disabled"}}
```

可选的两项：`VISION_*` 四个字段配好才支持图片提问（同样要 OpenAI 兼容）；
`RESEARCH_SERPAPI_API_KEY` 配好才会把联网工具下发给模型。

## 启动

```bash
./scripts/dev.sh
```

前端 `http://127.0.0.1:5173`，后端 `http://127.0.0.1:8000/api/v2/health`。
8000 或 5173 被占用时脚本会直接失败并给出 kill 命令——这是故意的，残留的旧进程会静默接管请求。

**不要用 `python -m uvicorn` 之类的命令自己起后端再起前端**，`dev.sh` 已经处理了端口检查、
reload 目录和退出时清理子进程。

首屏是登录页，输任意用户名即可。每个用户名对应一份独立的数据库与文件目录。
这不是身份认证，没有密码。

## 验证装好了

```bash
./scripts/check.sh
```

跑后端全部测试、Python 编译检查、前端类型检查与生产构建。全绿说明装对了。
这个命令不需要配 API key，也不发任何网络请求。

## 想要一份能直接玩的示例数据

```bash
./scripts/dev.sh                                   # 另开一个终端，保持运行
.venv/bin/python scripts/example_setup.py          # 建课 + 下载公开教材 + 建索引
```

跑完用 `example` 这个用户名登录，就有两门带教材和索引的课程可以直接提问。
教材是脚本从各自官网下载的公开教材切片，不在仓库里。

## 改代码时注意

- 改完跑 `./scripts/check.sh`，别只跑一部分。
- 后端测试要 `PYTHONPATH=backend`，项目里没有 pytest 配置文件。
- 数据库 schema 改动一律新增 migration，写在 `backend/core/store.py` 的 `MIGRATIONS` 末尾，
  不要改已有条目。
- 组装只在 `backend/app/bootstrap.py` 一处发生，模块自己不 new 仓储和适配器。
- 项目不含任何发布或部署操作，也不要加。
