# CoursePilot 2.0 Demo

个人开源学习助手的本地端到端 Demo。当前包含通用/课程会话、每轮课程解析（支持沿用近期解析结果）、带多轮历史与工具循环的流式 Agent 对话（每轮先用用户问题自动做一次课程证据检索作为种子，模型再按需自主调用检索/资料清单/计划/档案工具，带步数上限；引用跨工具去重编号）、课程隔离的混合检索（BGE 语义向量 + 词面，RRF 融合，支持中文问题命中英文教材）、带页码引用的 RAG 资料库、每轮对话落盘的 JSONL trace、可选 Wiki、学习计划与学习档案的只读接口骨架，以及在流式过程中展示“查了什么”的 React 前端。

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

## 验证

```bash
./scripts/check.sh
```

该命令运行完整后端测试、Python 编译检查、前端类型检查与生产构建。项目不包含任何发布或部署操作。

后端运行时可另开终端执行真实 HTTP smoke；该命令在远端开关启用时会产生一次 DeepSeek 调用：

```bash
.venv/bin/python scripts/smoke.py
```
