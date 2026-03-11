# CoursePilot — 大学课程学习 Agent（Multi-Agent + RAG + MCP）

![Python](https://img.shields.io/badge/Python-3.11-blue.svg) ![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg) ![Streamlit](https://img.shields.io/badge/Streamlit-1.31-red.svg) ![LLM](https://img.shields.io/badge/LLM-DeepSeek%20Chat%20%7C%20OpenAI-blueviolet.svg) ![FAISS](https://img.shields.io/badge/Vector%20DB-FAISS-orange.svg) ![License](https://img.shields.io/badge/license-MIT-lightgrey.svg)

> 将课程教材接入 RAG 知识库，三种模式闭环（学习→练习→考试），多 Agent 协同 + MCP 工具支撑，让大学生更快掌握课程内容。

---

## 速览
- 🎯 三种模式：学习讲解 / 智能出题+评分 / 模拟考试
- 📚 RAG：PDF/TXT/MD/DOCX/PPTX/PPT 解析，Hybrid 检索（FAISS + BM25），附页码引用
- 🛠️ 工具：计算器、网页搜索、文件写入、记忆检索、思维导图、日期时间查询（共 6 个 MCP 工具）
- 🧠 记忆系统：SQLite 跨会话追踪薄弱知识点，自动强化
- ⚡ 体验：SSE 流式输出 + 执行进度提示，Mermaid 思维导图渲染与 PNG 导出
- 🔒 安全：路径穿越防护、并发 chdir 加锁、编码回退、分块死循环保护

---

## 模式与工具

| 模式 | 适用场景 | 执行 Agent | 工具可用性（代码层） | 关键约束 | 自动记录 |
|------|----------|-----------|---------------------|----------|----------|
| **学习 (Learn)** | 概念讲解、知识梳理 | TutorAgent | `ToolPolicy` 放行全部 6 个工具（运行时按 Agent 规则约束） | 优先 RAG 引用，按需 ReAct 调用工具 | 问答写入 `memory.db`（`qa` episode） |
| **练习 (Practice)** | 出题、提交答案、评分讲评 | QuizMasterAgent（出题）+ GraderAgent（评分讲解） | 出题阶段：按需最小工具（主要 `websearch/get_datetime`）；评卷阶段：仅 `calculator` | `_is_answer_submission()` 命中后强制切到 Grader 评卷链路 | `practices/` Markdown 记录 + `memory.db` |
| **考试 (Exam)** | 模拟考试、自测报告 | QuizMasterAgent（出卷）+ GraderAgent（批改讲解） | 出卷阶段：按需最小工具（主要 `websearch/get_datetime`）；批改阶段：仅 `calculator` | `_is_exam_answer_submission()` 命中后进入 Grader 评卷链路 | `exams/` Markdown 记录 + `memory.db` |

- **学习模式**：基于上传教材 RAG 检索，支持引用来源展示、网页补充检索、思维导图生成与笔记落盘。
- **练习模式**：QuizMaster 负责出题（Plan-Solve + 按需外部信息），提交答案后 Runner 自动路由至 Grader，按“逐题对照 → calculator 汇总 → 讲评”流程输出并落盘。
- **考试模式**：QuizMaster 负责出卷（返回试卷正文 + 内部答案元数据），交卷后 Grader 负责批改与讲解；结束后自动保存考试记录并同步记忆。
- **实现说明**：当前 `core/orchestration/policies.py` 中 `learn/practice/exam` 的 `allowed_tools` 均为 `ALL_TOOLS`。模式差异主要来自 Runner 路由和 Agent 内部实现约束，不是白名单本身。
- **方法论标签**：Router=`Plan+Replan`，Tutor=`ReAct`，QuizMaster=`Plan-Solve`，Grader=`Plan + ReAct-Solve(calculator only)`。

### RAG 知识库

- 支持 **PDF / TXT / MD / DOCX / PPTX / PPT** 六种格式
- 文本分块 + FAISS 向量索引（嵌入模型：`BAAI/bge-base-zh-v1.5`，专为中文优化）
- 检索模式支持 `dense / bm25 / hybrid`，默认 `hybrid`（RRF 融合）
- GPU 自动加速：有 NVIDIA GPU 时自动使用 CUDA，batch_size 256；无 GPU 退回 CPU
- TXT/MD 文件自动检测编码（UTF-8 → GBK → Latin-1 回退）
- 检索结果携带文档名、页码、相关度分数
- `TOP_K_RESULTS` 控制默认返回数量；考试模式内部固定 `top_k=12`

### 多 Agent 编排

```
用户请求
   ↓
OrchestrationRunner（Python 调度器）
   ├─ [LLM] Router Agent  ← 制定 Plan（need_rag / style）并在失败时 Replan 一次
   ├─ [工具] RAG Retriever ← Hybrid 检索（FAISS + BM25）
   ├─ [工具] memory_search ← 预取历史错题上下文
   │
   ├─ 学习模式
   │      ↓
   │   Tutor Agent（ReAct）
   │      ├─ 可调用：calculator · websearch · filewriter
   │      │          memory_search · mindmap_generator · get_datetime
   │      └─ 流式输出教学回答
   │
   ├─ 练习/考试出题
   │      ↓
   │   QuizMaster Agent（Plan-Solve）
   │      ├─ 默认不走工具循环，仅按需调用 MCP（websearch/get_datetime）
   │      └─ 输出题目/试卷 + 内部元数据（quiz_meta / exam_meta）
   │
   └─ 练习/考试评卷（检测到答案提交）
          ↓
       Grader Agent（Plan + ReAct-Solve）
          ├─ 先做内部评卷计划（不展示）
          ├─ 仅调用 calculator 汇总得分
          └─ 流式输出评分结果 + 讲评
```

### MCP 工具集成

| 工具 | 功能 |
|------|------|
| `calculator` | 数学表达式计算（支持 `math`/`statistics`/组合数学/双曲函数/单位换算，Python 受限 `eval`） |
| `websearch` | SerpAPI 网页搜索（用于补充教材外信息；实际是否调用由当轮模式提示词与任务阶段决定） |
| `filewriter` | 将笔记写入课程 `notes/` 目录（`.md` 格式） |
| `memory_search` | 检索历史练习/错题记忆，自动强化薄弱知识点 |
| `mindmap_generator` | 生成 Mermaid 思维导图，支持导出 SVG / 3× 高清 PNG / 源码 |
| `get_datetime` | 返回当前精确日期、时间、星期，避免 LLM 凭训练数据回答时效性问题 |

说明：工具调用路径统一为 `OpenAI tool call -> MCPTools.call_tool -> stdio MCP -> server_stdio.py -> 本地工具实现`，不做本地直调 fallback。

#### MCP 实现细节（当前版本）

**1) 传输与协议**
- 传输层：本地 `stdio`（`Content-Length` 帧）；
- 协议子集：`initialize / notifications/initialized / tools/list / tools/call`；
- 协议版本：`2024-11-05`。

**2) Client 侧（`mcp_tools/client.py`）**
- `_StdioMCPClient` 作为单例客户端，按需懒启动 `python -m mcp_tools.server_stdio`；
- 启动后先握手：`initialize`，再发送 `notifications/initialized`；
- 每次 `tools/call` 使用 JSON-RPC 请求 ID 做请求/响应匹配；
- 进程异常时自动重连 1 次；进程退出时通过 `atexit` 清理子进程。

**3) Server 侧（`mcp_tools/server_stdio.py`）**
- `stdout` 仅输出协议帧，业务 `print` 重定向到 `stderr`，避免污染协议通道；
- `tools/list` 返回由 `_to_mcp_tools()` 转换后的 MCP 工具定义（`name/description/inputSchema`）；
- `tools/call` 调用 `MCPTools._call_tool_local()` 执行真实工具逻辑并返回结构化结果。

**4) 严格仅 MCP 语义**
- `MCPTools.call_tool()` 统一走 `mcp_stdio`，不再回退本地直调；
- 返回体附加 `via: "mcp_stdio"` 便于观测；
- 通道异常时返回标准错误结构（`success=false` + `error`），由上层 Agent 决定如何继续回答。

**5) 上下文透传**
- `filewriter` 的 `notes_dir` 由 Runner 注入后，通过 MCP 参数透传给子进程执行，保证写入当前课程目录。

### 实时流式输出

后端通过 **Server-Sent Events (SSE)** 逐 token 推送，前端 Streamlit 实时渲染，减少等待感。

- 前端会展示当前执行阶段（如“模型分析中 / 检索中 / 工具调用中 / 整理答案中”），避免“卡死感”。
- 每轮回答只展示当前轮检索到的引用来源；历史回答保留各自引用，不会混入当前轮引用。
- 为降低引用编号串扰，前端传历史给后端前会清理 assistant 历史中的 `[来源N]` 标记。

---

## 系统架构

```
Browser (Streamlit :8501)
    │  HTTP / SSE
    ▼
FastAPI (:8000)
    │
    ├─ OrchestrationRunner（Python 调度器，非 LLM）
    │    ├─ Router Agent      → Plan + Replan（need_rag / style）
    │    ├─ Tutor Agent       → 学习讲解（ReAct）
    │    ├─ QuizMaster Agent  → 练习出题 / 考试出卷（Plan-Solve）
    │    └─ Grader Agent      → 练习/考试评卷讲解（Plan + ReAct-Solve）
    │
    ├─ RAG Pipeline
    │    ├─ DocumentParser  (ingest.py)
    │    ├─ Chunker         (chunk.py)
    │    ├─ EmbeddingModel  (embed.py)
    │    └─ FAISSStore      (store_faiss.py)
    │
    └─ MCP Tools
         ├─ calculator
         ├─ websearch
         ├─ filewriter
         ├─ memory_search
         ├─ mindmap_generator
         └─ get_datetime
```

### Agent 职责
| Agent | 调用时机 | 输入 | 输出 |
|-------|---------|------|------|
| Router | 每次请求首先调用；必要时触发一次重规划 | 用户消息 + 模式 + 失败原因（重规划时） | `Plan`（need_rag、style、output_format） |
| Tutor | 学习模式主执行 | 问题 + RAG 上下文 + 历史 | 教学内容（可含引用/工具结果） |
| QuizMaster | 练习出题 / 考试出卷 | 请求 + RAG 上下文 + 历史错题上下文 | 题目/试卷正文 + 内部元数据 |
| Grader | 练习/考试检测到答案提交 | 题目或试卷原文 + 学生答案 + 历史错题上下文 | 逐题对照 + 得分 + 讲评 |

---

## 目录结构

```

├── README.md                 ← 本文档
├── docs/
│   ├── USAGE.md              ← 使用手册（面向用户）
│   ├── ARCHITECTURE.md       ← 架构设计与数据流
│   ├── debug.md              ← 调试记录
│   ├── CONTRIBUTING.md       ← 贡献指南
│   └── SECURITY.md           ← 安全说明
├── requirements.txt
├── pyproject.toml
├── .env                      ← 本地环境变量（不入库）
├── rebuild_indexes.py        ← 批量重建全部课程索引
│
├── frontend/
│   └── streamlit_app.py      ← Streamlit 前端
│
├── backend/
│   ├── api.py                ← FastAPI 路由、上传、SSE 端点
│   └── schemas.py            ← Pydantic 数据模型
│
├── core/
│   ├── llm/
│   │   └── openai_compat.py  ← LLM 客户端（兼容 DeepSeek / OpenAI）
│   ├── orchestration/
│   │   ├── runner.py         ← 主编排器 + 记录保存
│   │   ├── prompts.py        ← 提示词模板
│   │   └── policies.py       ← 工具策略（模式 → 允许工具）
│   └── agents/
│       ├── router.py
│       ├── tutor.py
│       ├── quizmaster.py
│       └── grader.py
│
├── rag/
│   ├── ingest.py             ← PDF/TXT/MD/DOCX/PPTX/PPT 解析
│   ├── chunk.py              ← 文本分块（含重叠保护）
│   ├── embed.py              ← sentence-transformers 嵌入
│   ├── lexical.py            ← BM25 词法检索
│   ├── store_faiss.py        ← FAISS 向量索引（线程安全）
│   └── retrieve.py           ← dense/bm25/hybrid 检索 + 引用格式化
│
├── mcp_tools/
│   ├── client.py             ← 工具实现 + schema + 调用路由
│   └── server_stdio.py       ← stdio MCP Server（最小协议实现）
│
├── memory/
│   ├── manager.py            ← 记忆检索/写入编排
│   └── store.py              ← SQLite 存储访问
│
├── tests/
│   ├── test_basic.py
│   └── sample_textbook.txt
│
└── data/
    ├── memory/
    │   └── memory.db         ← SQLite 记忆库（episodes/user_profiles）
    └── workspaces/
        └── <course_name>/
            ├── uploads/      ← 上传的原始文件
            ├── index/        ← FAISS 索引文件
            ├── notes/        ← FileWriter 保存的笔记
            ├── mistakes/     ← 错题本（mistakes.jsonl）
            ├── practices/    ← 练习记录（自动保存，Markdown）
            └── exams/        ← 考试记录（自动保存，Markdown）
```

---

## 快速开始

1) 环境准备
```bash
conda create -n study_agent python=3.11 -y
conda activate study_agent
git clone https://github.com/Eric-he-cn/your_AI_study_agent.git
cd your_AI_study_agent
pip install -r requirements.txt
```

2) 配置环境变量（项目根目录 `.env`）
```dotenv
# LLM（必填）
OPENAI_API_KEY=sk-xxxxxxxx
OPENAI_BASE_URL=https://api.deepseek.com      # 或 https://api.openai.com/v1
DEFAULT_MODEL=deepseek-chat                   # 或 gpt-4o 等

# RAG（可选，均有默认值）
EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5   # 中文优化嵌入模型
EMBEDDING_DEVICE=auto                   # auto/cuda/cpu
EMBEDDING_BATCH_SIZE=256                # GPU 推荐 128-512；CPU 推荐 32
CHUNK_SIZE=600
CHUNK_OVERLAP=120
TOP_K_RESULTS=6
RETRIEVAL_MODE=hybrid                     # dense / bm25 / hybrid
BM25_K1=1.5
BM25_B=0.75
HYBRID_RRF_K=60
HYBRID_DENSE_WEIGHT=1.0
HYBRID_BM25_WEIGHT=1.0
HYBRID_DENSE_CANDIDATES_MULTIPLIER=3
HYBRID_BM25_CANDIDATES_MULTIPLIER=3

# MCP（可选）
SERPAPI_API_KEY=your_serpapi_key
```

3) 启动服务
```bash
# 终端1：后端
python -m backend.api   # 端口 8000，Swagger: http://localhost:8000/docs

# 终端2：前端
streamlit run frontend/streamlit_app.py   # 端口 8501
```

4) 首次使用流程
```
① 侧边栏创建课程（课程名 + 学科标签）
② 选择课程 → 上传教材（PDF/TXT/MD/DOCX/PPTX/PPT）
③ 点击「构建索引」→ 等待完成（显示块数）
④ 选择模式（学习/练习/考试）开始对话
```

---

## 环境变量说明
| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `OPENAI_API_KEY` | ✅ | — | LLM API 密钥 |
| `OPENAI_BASE_URL` | ✅ | `https://api.openai.com/v1` | API 基础 URL |
| `DEFAULT_MODEL` | — | `gpt-3.5-turbo` | 对话模型名称 |
| `EMBEDDING_MODEL` | — | `BAAI/bge-base-zh-v1.5` | 嵌入模型（HuggingFace Hub ID） |
| `EMBEDDING_DEVICE` | — | `auto` | 计算设备：`auto` / `cuda` / `cpu` |
| `EMBEDDING_BATCH_SIZE` | — | `256`（GPU）/ `32`（CPU） | encode batch 大小 |
| `CHUNK_SIZE` | — | `600` | 文本分块大小（字符数） |
| `CHUNK_OVERLAP` | — | `120` | 分块重叠大小（需 < CHUNK_SIZE，建议 20%） |
| `TOP_K_RESULTS` | — | `6` | 默认检索返回块数（考试模式内部固定 `top_k=12`） |
| `RETRIEVAL_MODE` | — | `hybrid` | 检索模式：`dense` / `bm25` / `hybrid` |
| `BM25_K1` | — | `1.5` | BM25 参数 `k1` |
| `BM25_B` | — | `0.75` | BM25 参数 `b` |
| `HYBRID_RRF_K` | — | `60` | RRF 融合常数 |
| `HYBRID_DENSE_WEIGHT` | — | `1.0` | dense 分支融合权重 |
| `HYBRID_BM25_WEIGHT` | — | `1.0` | bm25 分支融合权重 |
| `HYBRID_DENSE_CANDIDATES_MULTIPLIER` | — | `3` | dense 候选池倍数（`top_k * 倍数`） |
| `HYBRID_BM25_CANDIDATES_MULTIPLIER` | — | `3` | bm25 候选池倍数（`top_k * 倍数`） |
| `SERPAPI_API_KEY` | — | — | SerpAPI 密钥（`websearch` 工具依赖，未配置时该工具返回失败并跳过） |
| `DATA_DIR` | — | `data/workspaces` | 课程数据根目录 |

---

## API 速查

完整接口见 `http://localhost:8000/docs`。

### 课程管理
```http
GET    /workspaces                          # 列表
POST   /workspaces                          # 创建  Body: {course_name, subject}
DELETE /workspaces/{course_name}            # 删除
```

### 资料管理
```http
POST   /workspaces/{course_name}/upload               # multipart/form-data 上传
POST   /workspaces/{course_name}/build-index          # 构建 FAISS 索引
GET    /workspaces/{course_name}/files                # 文件列表 + 索引状态
DELETE /workspaces/{course_name}/files/{filename}     # 删除单个文件
DELETE /workspaces/{course_name}/index                # 删除向量索引
```

### 对话
```http
POST   /chat                                 # 同步对话
POST   /chat/stream                          # SSE 流式对话
```
请求体示例：
```json
{
  "course_name": "线性代数",
  "mode": "learn",
  "message": "什么是矩阵的秩？",
  "history": [
    {"role": "user", "content": "上一条消息"},
    {"role": "assistant", "content": "上一条回复"}
  ]
}
```
SSE 每帧格式：`data: <JSON字符串>\n\n`，需 `json.loads()` 解码。  
流式中可能出现以下元事件：
- `{"__citations__": [...]}`：当前轮引用元数据（文档名/页码/分数/原文片段）
- `{"__status__": "..."}`：当前执行状态提示（检索/工具调用/答案整理）

---

## 技术栈
| 层次 | 技术 |
|------|------|
| 前端 | Streamlit 1.31 |
| 后端 | FastAPI 0.109 + Uvicorn |
| LLM | OpenAI SDK 兼容（DeepSeek / OpenAI / 本地 Ollama） |
| 嵌入 | sentence-transformers `BAAI/bge-base-zh-v1.5`（中文，768 维） |
| 嵌入加速 | PyTorch CUDA 12.8（CPU fallback） |
| 向量库 | FAISS |
| 文档解析 | PyMuPDF（PDF）、python-docx（DOCX）、python-pptx（PPTX）、pywin32+PowerPoint（PPT） |
| 数据校验 | Pydantic v2 |
| 工具搜索 | SerpAPI |
| 异步 | Python asyncio + SSE |

---

## 安全与可靠性
- **文件上传**：`basename` 净化 + 扩展名白名单（`.pdf .txt .md .docx .pptx .ppt`），阻断路径穿越与非法格式。
- **课程名称**：`get_workspace_path()` 强制 `basename`，拒绝 `../` 等非法输入。
- **FAISS 并发安全**：全局锁包裹 `os.chdir()` + FAISS 读写，避免并发修改工作目录。
- **分块安全**：`chunk.py` 自动收敛 `overlap >= chunk_size`，杜绝死循环。
- **编码回退**：TXT 解析按 `utf-8-sig → utf-8 → gbk → latin-1` 尝试，不再静默丢失内容。
- **流式稳健性**：SSE chunk 统一 JSON 编码，前端逐帧 `json.loads()`，防止换行截断。

---

## 已知限制
- 扫描版 PDF（图片）需先 OCR，当前不支持直接提取文字。
- `.ppt` 解析依赖本机安装 Microsoft PowerPoint（通过 COM 转换到 `.pptx`）。
- 嵌入模型默认 `BAAI/bge-base-zh-v1.5`（中文优化，768 维）；中英混排教材可换 `BAAI/bge-m3`（多语言，更慢）。
- 更换嵌入模型后需运行 `python rebuild_indexes.py` 重建所有课程索引（维度变化时旧索引不兼容）。
- FAISS 在 >100 万向量时性能下降，需考虑分片或 HNSW 方案。
- 设计为单机部署，多实例需额外处理索引共享与并发写。
- 网页搜索依赖 SerpAPI，未配置 `SERPAPI_API_KEY` 时工具静默跳过。
- Mermaid 思维导图 PNG 导出在极复杂图表（>50节点）时可能超出浏览器渲染限制。

---

## 贡献与许可
- 欢迎提交 Issue / PR，一起完善功能与安全性。
- 许可证：MIT License
- 作者：**Eric He** · 更新日期：2026-03-11

