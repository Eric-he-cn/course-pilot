# 安全说明（SECURITY）

本文档描述 CoursePilot 项目当前代码状态下的安全控制、已知风险与生产部署建议。

- 最后更新：2026-03-11
- 适用范围：当前仓库代码（开发/测试环境）
- 声明：本项目尚未进行独立第三方安全审计

---

## 1. 当前已实现的安全控制

### 1.1 路径与文件安全

1. 课程名路径净化  
`core/orchestration/runner.py` 的 `get_workspace_path()` 对 `course_name` 使用 `os.path.basename(course_name.strip())`，并拒绝 `.`/`..`，用于阻断路径穿越。

2. 上传文件名净化与类型白名单  
`backend/api.py` 的上传接口仅使用 `os.path.basename(file.filename)`，并限制扩展名为：
- `.pdf`
- `.txt`
- `.md`
- `.docx`
- `.pptx`
- `.ppt`

3. FileWriter 写入目录受限  
`mcp_tools/client.py` 的 `filewriter` 同样对文件名做 `basename`，并将写入目录绑定到当前课程 `notes/`（由 runner 注入 `notes_dir`）。

### 1.2 工具调用与权限控制

1. 统一 MCP 调用与运行时约束  
工具调用统一走 `MCPTools.call_tool -> mcp_stdio`，无本地 fallback；`core/orchestration/policies.py` 当前对三模式均放行 `ALL_TOOLS`，实际权限收敛由 Runner 路由与 Agent 内部实现（如 Grader 仅允许 `calculator`）保证。

2. 计算器受限执行环境  
`mcp_tools/client.py` 的 `calculator` 使用受限 `eval`：
- `__builtins__` 为空
- 仅暴露预定义数学/统计函数

### 1.3 RAG 与并发稳定性保护

1. 分块死循环保护  
`rag/chunk.py` 对 `overlap >= chunk_size` 做修正，并在窗口推进异常时强制前移，避免无限循环。

2. 文本编码回退  
`rag/ingest.py` 对 TXT/MD 采用 `utf-8-sig -> utf-8 -> gbk -> latin-1` 回退，降低解析失败风险。

3. FAISS 并发 `chdir` 加锁  
`rag/store_faiss.py` 在 `save/load` 的 `os.chdir` 区域使用全局锁，避免并发请求互相污染工作目录。

### 1.4 数据隔离与存储

1. 课程级隔离目录  
数据按课程隔离在 `data/workspaces/<course_name>/` 下，上传文件、索引、笔记、练习/考试记录分别落在子目录。

2. 记忆库使用参数化 SQL  
`memory/store.py` 使用 SQLite 并采用参数化查询（`?` 占位），降低 SQL 注入风险。

### 1.5 接口层

1. SSE 输出做 JSON 包装  
`backend/api.py` 的 `/chat/stream` 以 JSON 编码 chunk，减少换行/特殊字符破坏 SSE 协议的风险。

2. CORS 当前为全开放  
`allow_origins=["*"]` 仅适合开发阶段，生产需收敛白名单。

---

## 2. 已修复的关键问题（历史）

以下问题已在代码中修复：

1. FastAPI `Content-Type` ReDoS 风险（升级到 `fastapi>=0.109.1`）  
2. 前端历史消息重复拼接导致上下文冗余/泄漏风险（`chat_history[-21:-1]`）  
3. 练习/考试记录中用户答案抓取错误（改为显式传入 `user_message`）  
4. 上传接口路径穿越（文件名 `basename` + 扩展名白名单）  
5. 课程名路径穿越（`get_workspace_path` 净化）  
6. 分块 overlap 异常导致潜在死循环（分块推进保护）  
7. TXT 单编码解析失败（多编码回退）  
8. FAISS `os.chdir` 并发不安全（加锁）

---

## 3. 当前已知风险与限制

1. 缺少认证与授权  
当前 API 未内置用户身份认证、权限控制与租户隔离策略，不适合直接公网暴露。

2. CORS 过宽  
后端允许所有来源跨域访问，生产环境存在被第三方站点滥用风险。

3. 缺少限流与配额控制  
未对聊天、上传、建索引等高成本接口进行速率限制与配额管理。

4. 上传安全基线不足  
目前无统一文件大小限制、MIME 深度校验、恶意文件扫描（如病毒/宏）。

5. Prompt Injection 风险仍存在  
用户输入、上传文档和网页检索内容均可能包含提示注入指令；当前主要依赖提示词约束，尚无系统级隔离策略。

6. `calculator` 仍有资源滥用风险  
虽然限制了 `builtins`，但超大规模数学表达式（如极端组合数/阶乘）仍可能造成 CPU 压力。

7. 依赖版本源不统一  
`requirements.txt` 与 `pyproject.toml` 存在版本差异（如 `openai`、`faiss-cpu`），建议统一锁定策略，避免不同安装路径带来不可预测行为。

8. Secrets 管理仍偏开发态  
默认依赖本地 `.env`，生产环境应迁移到专业密钥管理服务。

---

## 4. 生产部署加固清单

上线前建议至少完成以下项：

- [ ] 启用认证（API Key/JWT/OAuth2）与细粒度授权
- [ ] 收敛 CORS 白名单到可信域名
- [ ] 为 `/chat`、`/chat/stream`、`/upload`、`/build-index` 加限流与并发控制
- [ ] 限制上传文件大小、类型与数量；增加 MIME 校验与恶意文件扫描
- [ ] 对高成本操作设置超时、熔断与队列（特别是建索引/大模型调用）
- [ ] 增加输入长度与内容策略（防提示注入、防资源滥用）
- [ ] 统一依赖锁定（推荐固定到单一 lock 文件/镜像）
- [ ] 使用 HTTPS/TLS、反向代理与安全响应头
- [ ] 接入审计日志、告警与异常监控
- [ ] 将密钥迁移到 KMS/Secrets Manager，禁用明文环境文件分发
- [ ] 为数据目录做备份、恢复演练与磁盘配额管理

---

## 5. 漏洞报告

如发现安全漏洞，请遵循负责任披露：

1. 不要在公开 Issue 直接披露可利用细节  
2. 通过维护者私下渠道提交漏洞信息（影响范围、复现步骤、修复建议）  
3. 给予修复与发布缓冲时间后再公开技术细节

---

## 6. 维护建议

建议在以下场景触发一次安全复核：

1. 升级核心依赖（FastAPI、OpenAI SDK、FAISS、Streamlit）后  
2. 新增 MCP 工具或放宽工具权限策略后  
3. 变更文件上传、索引构建、存储路径处理逻辑后  
4. 准备公网部署前
