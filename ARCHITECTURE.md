# Course Learning Agent - 项目说明

## 🎯 项目概述

这是一个完整的 **AI 课程学习助手** 项目，专门为大学生课程学习设计。与通用 AI 助手不同，本系统提供：

1. **基于教材的 RAG 系统** - 所有回答都有教材引用
2. **三种学习模式** - 学习、练习、考试
3. **多 Agent 协作** - Router（规划）、Tutor（教学/出题/考试）、Grader（练习评卷）
4. **工具可控集成** - 不同模式限制不同工具
5. **持久化记忆系统** - SQLite 存储学习历史与薄弱知识点
6. **完整学习闭环** - 从理解到练习到考试

## 📐 系统架构设计

### 1. 整体架构

```
┌─────────────────────────────────────────────────────┐
│                   Streamlit Frontend                 │
│    (课程选择 | 模式切换 | 对话界面 | 文件管理)         │
└────────────────────┬────────────────────────────────┘
                     │ HTTP / SSE
┌────────────────────▼────────────────────────────────┐
│                  FastAPI Backend                     │
│      (Workspace管理 | 文件上传 | 索引管理 | 对话)     │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────┐
│       OrchestrationRunner                           │
│  ────────────────────────────────────────────────── │
│  [LLM] Router Agent → Plan（need_rag / style）      │
│  [工具] RAG Retriever → FAISS 检索 + 引用            │
│  [工具] memory_search → 历史错题预取                 │
│  ────────────────────────────────────────────────── │
│  路由判断（Python 关键词检测）                        │
│       ┌──────────────────────┐                      │
│       │  学习 / 考试 / 练习出题│                      │
│       ▼                      ▼                      │
│  ┌──────────┐       ┌──────────────┐               │
│  │  Tutor   │       │练习答案提交时 │               │
│  │  Agent   │       │   Grader     │               │
│  │ (ReAct)  │       │   Agent      │               │
│  └────┬─────┘       │  (ReAct)     │               │
│       │             └──────┬───────┘               │
└───────┼────────────────────┼────────────────────────┘
        │                    │
   ┌────▼────┐          ┌───▼────┐
   │  RAG   │          │ MCP    │  ← 工具：calculator /
   │ System │          │ Tools  │    websearch / filewriter /
   └────────┘          └────────┘    memory_search / mindmap / datetime
```

### 2. 核心模块说明

#### A. RAG 系统 (rag/)

**功能**: 文档检索增强生成

**流程**:
```
PDF/TXT/MD/DOCX/PPTX/PPT → 解析 → 分块 → Embedding → FAISS 索引
                                          ↓
用户查询 → Embedding → 相似度检索 → Top-K 结果 → 带引用的上下文
```

**关键文件**:
- `ingest.py`: 文档解析 (支持 PDF, TXT, MD, DOCX, PPTX, PPT)
- `chunk.py`: 文本分块 (滑窗 + overlap)
- `embed.py`: 向量嵌入 (Sentence Transformers)
- `store_faiss.py`: FAISS 向量存储（平铺文件 `.faiss` + `.pkl`，线程锁保护）
- `retrieve.py`: 检索 + 引用生成

#### B. Agent 系统 (core/agents/)

**Router Agent** (router.py)
- 职责：分析用户请求，制定执行计划（每次请求的第一个 LLM 调用）
- 输入：用户消息、模式、课程信息、用户学习档案（薄弱知识点）
- 输出：`Plan` 对象（`need_rag`、`style` 字段有效；工具列表由 `policies.py` 硬编码覆盖）
- **实际局限**：`allowed_tools` 和 `task_type` 被 Runner 强制覆盖，LLM 只有效影响 `need_rag` 和 `style`

**Tutor Agent** (tutor.py)
- 职责：学习讲解 / 练习出题 / 考试三阶段（所有非评卷场景）
- 输入：问题 + RAG 上下文 + 对话历史 + system_prompt_override + user_content_override
- 输出：流式文本（ReAct 循环，多轮工具调用）
- 特点：接受 system/user prompt 完全覆盖，让同一个 Agent 适配三种模式；注入用户薄弱知识点画像
- 工具（学习/练习出题）：全部 6 个；工具（考试）：calculator · memory_search · get_datetime

**Grader Agent** (grader.py) — 练习评卷专用
- 职责：仅在检测到答案提交时由 Runner 路由调用，专职评卷
- 输入：题目原文（从对话历史提取） + 学生答案原文 + 历史错题上下文
- 输出：流式文本（逐题对照表 + calculator 得分 + 讲评）
- ReAct 规则：第1轮必须逐字引用双方答案再判断对错；第2轮必须 call calculator；第3轮输出结果
- Temperature=0.1（评判高确定性）；仅开放 calculator 工具

**QuizMaster Agent** (quizmaster.py) — 当前未启用
- 保留代码，预埋扩展接口；主流程不调用，出题功能由 TutorAgent 承担

#### C. MCP 工具 (mcp_tools/)

**工具集**:

| 工具 | 学习 | 练习出题 | 练习评卷 | 考试 | 功能 |
|------|:----:|:------:|:------:|:----:|------|
| `calculator` | ✅ | ✅ | ✅（必须） | ✅（必须） | 数学计算（math/statistics/组合数学/双曲函数/单位换算） |
| `websearch` | ✅ | ✅ | ❌ | ❌ | SerpAPI 网页搜索 |
| `filewriter` | ✅ | ✅ | ❌ | ❌ | 写入 `notes/` 笔记（`.md` 格式） |
| `memory_search` | ✅ | ✅ | ❌ | ✅ | 检索历史练习/错题，自动强化薄弱点 |
| `mindmap_generator` | ✅ | ✅ | ❌ | ❌ | 生成 Mermaid 思维导图，支持 SVG/PNG 导出 |
| `get_datetime` | ✅ | ✅ | ❌ | ✅ | 查询精确日期时间，避免 LLM 凭训练数据猜测 |

> 练习评卷由 GraderAgent 独立处理，仅开放 calculator，确保评判聚焦、结果确定。

**工具策略** (policies.py)：
```python
# 所有模式共享全部工具集（policies.py commit 91783f2）
# 工具的实际使用范围由各 Agent 的 system prompt 约束
ALL_TOOLS = ["calculator", "websearch", "filewriter", "memory_search", "mindmap_generator", "get_datetime"]
```

#### D. 记忆系统 (memory/)

**存储**: SQLite，路径 `data/memory/memory.db`

**表结构**:
- `episodes`: 每次练习/考试/错题的详细记录（timestamp、course、type、content、score）
- `user_profiles`: 每门课程的薄弱知识点聚合（weak_points、practice_count、avg_score）

**写入时机**:
- 练习评分完成 → `_save_grading_to_memory()` → 写 `practice`/`mistake` episode
- 考试批改完成 → `_save_exam_to_memory()` → 写 `exam` episode
- 每次写入自动调用 `update_weak_points()` + `record_practice_result()` 更新用户画像

**用户画像注入**: Tutor/QuizMaster 在 system prompt 中自动附加弱点列表，优先针对薄弱知识点讲解和出题。

### 3. 数据流详解

#### 学习模式流程

```
用户: "什么是矩阵的秩?"
  ↓
FastAPI (/chat/stream) → SSE 流式响应
  ↓
Runner.run_learn_mode_stream()
  ↓
[LLM #1] RouterAgent.plan() → Plan(need_rag=True, style="step_by_step")
  ↓
[工具] Retriever.retrieve("矩阵的秩") → [教材片段1, 片段2, ...]（含页码）
  ↓
[LLM #2~N] TutorAgent.teach_stream()  ← ReAct 循环
  system: TUTOR_PROMPT + 用户学习档案（薄弱知识点）
  tools:  全部 6 个工具
  │
  ├─ 可能 call websearch / mindmap_generator / calculator
  └─ 流式输出：核心答案 + 详细解释 + [来源N] 引用
```

#### 练习模式流程

```
【出题阶段】
用户: "给我出一道矩阵秩的题"
  ↓
Runner.run_practice_mode_stream()
  ↓
[LLM #1] RouterAgent.plan()
  ↓
[工具] Retriever.retrieve() → RAG 上下文
[工具] memory_search()      → 历史错题片段（追加到 system prompt）
  ↓
_is_answer_submission() → False（用户在请求出题）
  ↓
[LLM #2~N] TutorAgent.teach_stream()  ← ReAct 循环
  system: PRACTICE_SYSTEM + 历史错题上下文
  user:   PRACTICE_PROMPT（对话式练习规则）
  tools:  全部 6 个工具
  │
  └─ 可能 call websearch 查找补充题材 → 流式输出题目

【评卷阶段】
用户: "1.A  2.正确  3.B..."（提交答案）
  ↓
Runner.run_practice_mode_stream()
  ↓
[LLM #1] RouterAgent.plan()
[工具] memory_search() → 历史错题上下文
  ↓
_is_answer_submission() → True（检测到答案格式）
_extract_quiz_from_history() → 从对话历史中提取题目原文（纯 Python，无 LLM）
  ↓
[LLM #2~N] GraderAgent.grade_practice_stream()  ← ReAct 循环
  system: GRADER_SYSTEM（强制逐字引用原文规则）
  user:   GRADER_PRACTICE_PROMPT（题目 + 学生答案）
  tools:  仅 calculator，temperature=0.1
  │
  ├─ 轮1：逐题对照表（原文引用标准答案 vs 学生答案，判断对错）
  ├─ 轮2：call calculator('sum([20, 15, 0, 15, 10])')
  └─ 轮3：流式输出得分 + 各题讲评 + 易错提醒
  ↓
_save_practice_record()   → 写 practices/ Markdown 文件
_save_grading_to_memory() → 写 SQLite（episodes + user_profiles）
```

#### 考试模式流程

```
用户: "来一套线性代数综合测试"
  ↓
Runner.run_exam_mode_stream()
  ↓
[LLM #1] RouterAgent.plan()
  ↓
[工具] Retriever.retrieve(top_k=12) → 大范围 RAG 上下文
  ↓
[LLM #2~N] TutorAgent.teach_stream()  ← ReAct 循环（三阶段对话）
  system: EXAM_SYSTEM（三阶段规则）
  user:   EXAM_PROMPT
  tools:  calculator · memory_search · get_datetime
  │
  ├─ 阶段一：对话收集配置（题型/题数/难度）
  ├─ 阶段二：生成完整试卷（不透露答案）
  └─ 阶段三：学生提交后，逐题批改 + call calculator + 输出总分
  ↓
_is_exam_grading() → True → _save_exam_record() + _save_exam_to_memory()
```

## 🎨 前端界面设计

### 布局结构

```
┌────────────────────────────────────────────────────┐
│  课程学习助手 📚                                      │
├─────────────┬──────────────────────────────────────┤
│  侧边栏      │  [课程名] [模式徽章]  [❓帮助] [🗑历史] │
│             │  ────────────────────────────────    │
│ [课程选择]   │  ┌ 模式指示条 ──────────────────── ┐  │
│  线性代数    │  │ 📖 学习模式  基于教材精准讲解…   │  │
│  通信原理    │  └───────────────────────────────── ┘  │
│  + 新建      │                                        │
│             │  💬 对话区                              │
│ [模式选择]   │  User: 什么是矩阵的秩？               │
│  ○ 学习     │  Assistant: [回答内容]                │
│  ○ 练习     │    📑 查看引用 ▼                      │
│  ○ 考试     │    🔧 工具调用 ▼                      │
│             │    🗺 [Mermaid 思维导图交互图]         │
│ [📁文件与索引]│    [⬇ SVG] [⬇ PNG] [⬇ .mmd]        │
│  file1.pdf  │                                        │
│  file2.pptx │  [输入框: 输入你的问题...]              │
│  🔨 构建索引  │                                        │
│  🗑 删除文件  │                                        │
│  🗑 删除索引  │                                        │
└─────────────┴──────────────────────────────────────┘
```

### 交互特性

1. **模式主题色**: 学习=蓝色、练习=绿色、考试=琥珀色（模式徽章 + 左侧指示条）
2. **Mermaid 思维导图**: 内嵌交互渲染（前端 HTML 组件），支持缩放；3× 超采样 PNG 导出
3. **帮助面板**: ❓ 按钮切换，内嵌快速开始指南
4. **文件管理**: 侧边栏显示文件大小/日期，支持单独删除文件、删除索引
5. **清空历史**: 主区域一键清空对话历史
6. **实时流式**: SSE 逐 token 推送，前端 Streamlit 实时拼接渲染

## 📊 数据存储结构

```
data/
├── memory/
│   └── memory.db              # SQLite 记忆库（episodes + user_profiles）
└── workspaces/
    └── 线性代数/
        ├── uploads/           # 原始文档
        │   ├── 教材第一章.pdf
        │   ├── 课堂讲义.txt
        │   └── 思维导图.pptx
        ├── index/             # 向量索引（平铺文件，非目录）
        │   ├── faiss_index.faiss
        │   └── faiss_index.pkl
        ├── notes/             # AI 保存的 Markdown 笔记
        ├── mistakes/          # 错题本
        │   └── mistakes.jsonl
        ├── practices/         # 练习记录
        │   └── practice_20260222_143000.json
        └── exams/             # 考试记录
            └── exam_20260222_160000.json
```

### mistakes.jsonl 格式

```json
{"timestamp": "2026-02-22T10:30:00", "question": "...", "student_answer": "...", "score": 75, "feedback": "...", "mistake_tags": ["步骤缺失"]}
```

## 🔧 技术实现要点

### 1. RAG 实现

**分块策略**:
```python
chunk_size = 600      # 字符数（中文密度高）
overlap = 120         # 重叠字符数（≈20%，防止术语跨块截断）
```

**嵌入策略**:
```python
model = "BAAI/bge-base-zh-v1.5"   # 中文专用，768 维
device = "cuda"  # 或 "cpu"（auto-detect via torch.cuda.is_available()）
batch_size = 256  # GPU；CPU 时降为 32
```

**检索策略**:
```python
top_k = 6            # 返回前6个最相关片段
similarity = L2      # normalize_embeddings=True 等价余弦
```

### 2. Prompt Engineering

- **system/user override**：TutorAgent 的 `teach_stream()` 接受 `system_prompt_override` 和 `user_content_override`，让同一 Agent 适配学习/练习/考试三种角色，无需多份执行代码
- **PRACTICE_SYSTEM / EXAM_SYSTEM / GRADER_SYSTEM**：模式专用 system prompt 常量，在 `prompts.py` 集中管理
- **CoT 强制评分**：GraderAgent 的 `GRADER_PRACTICE_PROMPT` 三步走：先逐字引用 → 判断对错 → calculator 汇总，禁止跳步
- **证据优先**：学习模式 Tutor 强制引用教材 `[来源N]` 内联标注
- **结构化输出**：Router 输出 JSON Plan；考试批改输出固定 Markdown 表格格式

### 3. 错误处理

- LLM 调用失败 → 返回错误消息
- JSON 解析失败 → 使用默认值
- 文件上传失败 → 前端提示
- 索引不存在 → 提示用户先建索引
- 记忆库写入失败 → 静默跳过，不影响主流程

### 4. 可扩展性设计

**新增 Agent**:
```python
# 1. 在 core/agents/ 创建新 agent 文件
# 2. 在 prompts.py 添加 prompt 模板
# 3. 在 runner.py 添加调用逻辑
```

**新增工具**:
```python
# 在 mcp_tools/client.py 添加 schema + 实现 + call_tool 路由
# 在 policies.py 按模式配置允许列表
# 在 tutor.py system prompt 添加使用规则
@staticmethod
def new_tool(param):
    return {"tool": "new_tool", "result": ..., "success": True}
```

**新增模式**:
```python
# 1. 在 schemas.py 添加到 Literal 类型
# 2. 在 policies.py 配置工具策略
# 3. 在 runner.py 添加模式处理逻辑
```

## 🎯 核心创新点

### 1. 证据优先架构
- 强制要求引用教材来源，显示页码和文档名

### 2. 学习闭环设计
```
理解 (Learn) → 练习 (Practice) → 检测 (Exam) → 复习 (错题本+记忆库)
     ↑                                                    │
     └────────────── memory_search 弱点反馈 ───────────────┘
```

### 3. 工具策略控制
- 不同模式不同策略，考试模式防作弊，所有调用可观测

### 4. Agent 职责分离
- Router：规划（need_rag / style）
- Tutor：学习讲解 · 练习出题 · 考试三阶段（ReAct，全工具集）
- Grader：练习评卷专用（ReAct，仅 calculator，逐字引用原文对比，temperature=0.1）
- OrchestrationRunner：硬编码 Python 调度器，串联 RAG + 记忆 + Agent + 持久化，本身不是 LLM

### 5. 持久化记忆
- SQLite 跨会话追踪薄弱点，AI 自动在薄弱知识点上加强

## 🚀 部署建议

### 开发环境
```bash
conda activate study_agent
python -m backend.api                        # 后端: localhost:8000
streamlit run frontend/streamlit_app.py      # 前端: localhost:8501
```

### 生产环境
```bash
gunicorn backend.api:app -w 4 -k uvicorn.workers.UvicornWorker
# nginx 反代 → 8000
```

## 🔒 安全考虑

1. **API Key 保护**: 使用环境变量，不提交到代码库
2. **文件上传限制**: 白名单类型（PDF/TXT/MD/DOCX/PPTX/PPT），`basename()` 防路径穿越
3. **表达式执行**: Calculator 使用限定命名空间的 `eval`，无内建函数
4. **数据隔离**: 每个课程独立工作空间，课程名经 `basename()` 清洗
5. **FAISS 线程安全**: 模块级 `threading.Lock` 保护 `os.chdir()` 区域
6. **历史截断**: 对话历史仅取最近 20 条，防止 token 爆炸与数据泄漏

## 📚 调试技巧

1. 查看后端日志了解 API 调用和工具调用链
2. 查看 LLM 返回的原始文本（Runner 日志）
3. 检查 `data/workspaces/<course>/index/faiss_index.faiss` 是否存在
4. 检查 `data/memory/memory.db` 的 `episodes` 表确认记忆是否写入
5. 验证 `.env` 配置是否正确

---

## 💡 核心价值总结

✅ **产品化的学习系统** - 完整的学习闭环  
✅ **可控的 Agent 应用** - 工具策略 + 模式设计  
✅ **可追溯的知识系统** - 证据优先 + 引用标注  
✅ **持久化记忆增强** - 跨会话弱点追踪与强化  
✅ **可扩展的架构** - 模块化 + Agent 编排  