# CoursePilot 2.0 Demo

个人开源学习助手的本地端到端 Demo。当前包含通用/课程会话、每轮课程解析、SSE 对话、课程隔离的教材检索、RAG 资料库、可选 Wiki 和 React 前端。

## 本地启动

要求 Python 3.11+ 与 pnpm。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd frontend && pnpm install && cd ..
./scripts/dev.sh
```

浏览器打开 `http://127.0.0.1:5173`。后端健康检查位于 `http://127.0.0.1:8000/api/v2/health`。

真实 DeepSeek Adapter 已接入：当 `.env` 同时配置 `TEXT_API_KEY`、`TEXT_MODEL=deepseek-v4-flash` 和 `COURSEPILOT_ENABLE_REMOTE_LLM=1` 时，课程解析和 RAG 命中后会调用 DeepSeek；未启用、缺少证据或供应商失败时使用有明确状态的本地 fallback。仓库示例配置仍默认关闭远端调用，避免误耗额度。教材支持 PDF/TXT/MD，默认上限 100 MiB；图片附件上限仍是 10 MiB。

## 验证

```bash
./scripts/check.sh
```

该命令运行完整后端测试、Python 编译检查、前端类型检查与生产构建。项目不包含任何发布或部署操作。

后端运行时可另开终端执行真实 HTTP smoke；该命令在远端开关启用时会产生一次 DeepSeek 调用：

```bash
.venv/bin/python scripts/smoke.py
```
