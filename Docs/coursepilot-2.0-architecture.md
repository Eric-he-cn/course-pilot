# CoursePilot 2.0 架构设计

对应产品设计见 [coursepilot-2.0.md](coursepilot-2.0.md)。本文确定技术选型与系统架构。设计参照了 Claude Code、Codex、opencode、Hermes Agent 四个主流 Agent 产品的源码调研结论。

## 1. 设计原则

1. **单服务进程优先**：主形态是本地部署，一个 Python 服务进程承载后端能力，不引入消息队列、微服务或容器编排。耗时教材解析使用受控的 worker process，不占用 asyncio 事件循环。
2. **证据事件是定量学习状态的唯一事实源**：掌握度、错题与复习排期先落 append-only 事件，当前值是事件投影，可重放、可审计。会话消息、计划、Markdown 内容和调度任务有各自明确的存储语义，不笼统称为事件投影。
3. **LLM 负责判断与内容，关键副作用走确定性代码**：模型生成讲解、题目、评分和计划提案，也自行判断练习处于出题、作答还是讲评阶段；代码只硬校验跨模块 envelope、证据事件、计划日期、权限和幂等，不把教学流程写成状态机。
4. **自研 Agent 循环，不用编排框架**：主循环本身只有几百行，LangGraph 这类框架带来的抽象成本大于收益。四个参考产品全部是自研循环。
5. **上下文按需组装、原始历史永不修改**：发请求时做读时投影（裁剪、摘要），存储层始终保留全量原文。
6. **定性记忆用 Markdown，定量状态用 SQLite**：画像、情景记忆是人类可读的 markdown 文件（git 版本化）；掌握度、错题、排期是事件流投影。Markdown 源文件不持久化掌握度数字；前端展示 Wiki 时再从 SQLite 读取并动态渲染。
7. **会话 scope 与工具 scope 分离**：会话可为默认 `general` 或固定 `course`。课程会话的 `course_id` 生命周期内不变；通用会话每轮先由服务端 Course Resolver 产生受控 `ResolvedCourseContext`。工具永远只接受运行时注入的解析结果，不接受模型自由填写课程路径。
8. **模块化单体、接口优先**：课程、会话、Skill、Wiki、学习档案、计划、渠道各自拥有数据和服务边界；模块只能依赖公开 Protocol/DTO 或类型化领域事件，禁止跨模块导入实现类、直接查对方表或层层回调。

## 2. 技术选型总表

| 领域 | 选型 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.11+ | RAG 资产直接复用；BKT/FSRS 生态（py-fsrs）；多模态调用只是 API 请求 |
| Web 框架 | FastAPI + SSE | 1.0 已验证，异步生态成熟 |
| LLM 接入 | 自有统一协议 + provider adapter | 参考 1.0 `core/llm/openai_compat.py`，但不让 OpenAI SDK 响应结构泄漏到 Agent；文本与视觉模型独立配置 |
| 结构化存储 | SQLite（WAL 模式），单文件 `data/coursepilot.db` | 存放事件流、会话、计划、调度任务；零运维，量级远够 |
| SQLite 访问层 | 标准库 `sqlite3` + typed repository + 显式 migration | 单文件不需要 ORM；阻塞调用统一放入 worker thread，写事务在 Store 层串行化 |
| 记忆/Wiki 存储 | Markdown 文件 + git（GitPython） | user.md / memory.md / wiki 页面；版本、diff、回滚免费；人类可读可编辑 |
| 向量检索 | BGE 向量（sentence-transformers）+ SQLite FTS 词面，RRF 融合 | 沿用 1.0 的模型与融合策略；万级 chunk 用 numpy 暴力点积即可，FAISS 与 rerank 留待规模需要 |
| 复习排期 | py-fsrs | FSRS 官方 Python 实现，不自研排期算法 |
| BKT | 自实现（约 30 行贝叶斯更新） | 固定参数的经典四参数 BKT 就是几行公式，pyBKT 是为拟合大数据集设计的，用不上 |
| 定时调度 | APScheduler 单一 interval tick（无持久化 job store） | 每分钟扫描到期条目，计划变更不需要增删大量 job；SQLite 中的计划与 delivery 才是真源 |
| IM 渠道 | IM 平台官方 SDK，WebSocket 长连接模式 | 长连接不需要公网回调地址，本地部署可直接跑 |
| 前端 | React + Vite + TypeScript（SPA） | 替换 Streamlit；会话列表、wiki 浏览、计划视图需要真实前端；构建产物由 FastAPI 静态托管 |
| OCR | Vision LLM 结构化转录 + 文本主模型讲解 | 转录与推理分步，便于置信度门控、用户确认和独立评测 |
| Trace | 自研薄封装，JSONL 落盘 | 单用户本地产品不上 OpenTelemetry 全家桶 |

## 3. 进程结构

```
┌────────────────────── 单 Python 进程（asyncio） ──────────────────────┐
│                                                                      │
│  FastAPI HTTP/SSE ──┐                                                │
│  IM 长连接 client ──┼──→ 渠道抽象层 ──→ 通用/课程会话 → Agent 核心 │
│                              │              │            ├ RAG 服务   │
│  Scheduler tick ─────────────┘              │            ├ 档案服务   │
│  （每分钟扫描到期项并进入隐藏系统会话）          │            ├ 计划服务   │
│                                             │            ├ 记忆服务   │
│                                             │            └ Wiki 服务  │
│  Index worker process        存储：SQLite + git + FAISS + JSONL trace │
└──────────────────────────────────────────────────────────────────────┘
```

- IM 渠道的长连接作为 asyncio task 与 FastAPI 同进程运行，随进程启停；首版只接一个 IM，其余渠道只保留 `Channel` 接口。
- 渠道入站消息必须先解析到 `session_id + scope_mode`。Web 默认创建通用会话，也可进入固定课程工作区；IM 渠道每个用户只使用一个通用会话。通用会话在每轮 Agent 执行前解析相关课程，解析不唯一时进入澄清回复，不执行课程工具。
- Scheduler 每分钟执行一次 tick，查询到期且未投递的计划/复习项；它不直接调 LLM，而是把系统消息投进每门课程唯一的隐藏系统会话。系统会话不出现在用户会话列表、使用独立 turn lock，因此不会与用户正在聊天的 session 抢锁。
- FAISS 查询、GitPython 操作和 sqlite3 调用使用 `asyncio.to_thread`；PDF 解析、切块与索引构建进入 `ProcessPoolExecutor(max_workers=1)`。任何可能超过 100 ms 的同步调用都不得直接运行在事件循环。
- 服务器化路径：同一进程部署到服务器 + 前端改为纯静态托管，代码不变。

### 3.1 模块边界与依赖方向

```text
app / web / im / scheduler
              ↓
       application use cases
              ↓
courses | conversations | skills | artifacts | wiki | learning | planning
              ↓                         ↑
          ports / DTOs / typed domain events
              ↓                         ↑
     SQLite | RAG | Git | LLM | OCR adapters
```

- `modules/<feature>` 只暴露 `api.py`（用例接口）、`models.py`（稳定 DTO）和 `events.py`；实现类、repository 与表结构是模块私有。
- 跨模块同步请求通过 Protocol 端口；一对多联动通过带版本的类型化事件，如 `EvidenceRecorded`、`PlanChanged`、`MaterialIndexed`。需要可靠投递的事件与业务变更同事务写入 SQLite outbox，后台 dispatcher 成功后标记完成；handler 失败按退避时间重试并记 trace，不反向调用发布者。纯 UI 通知可以是进程内 best-effort 事件。
- `app/bootstrap.py` 是唯一 composition root，负责注入 adapter；业务模块不得自行构造数据库、LLM、Git 或渠道 client。
- CI 使用 import boundary test（可用 `import-linter`）禁止越层依赖；代码 review 固定检查“是否绕过公开接口、是否直接访问别的模块数据、是否把副作用藏在回调中”。

## 4. Agent 核心

### 4.1 通用/课程会话与 Course Resolver

`sessions.scope_mode` 为 `general | course`。课程会话要求非空 `course_id` 且创建后不可修改；通用会话的 `course_id` 必须为空，但保存最近一次可靠的 `resolved_course_id` 作为列表投影，不把它提升为永久绑定。

每轮先调用确定性的 `CourseResolverPort.resolve(message, session_summary, attachment_refs, candidate_courses)`，优先级为：用户本轮明确课程名/别名 → 附件所属课程 → 课程会话固定绑定 → 通用会话近期可靠解析 → 候选课程检索分数。只在唯一候选超过阈值时产生 `ResolvedCourseContext(course_id, confidence, reasons)`；否则返回 `needs_clarification`。模型可以生成澄清文案，但不能自行构造解析结果。

`ResolvedCourseContext` 是服务端上下文，不是模型参数。`rag_search`、`wiki_read/write`、`archive_query`、`artifact_read/append` 等工具从调用上下文取课程，JSON Schema 中不暴露 `course_id`。通用模式允许不同轮次解析到不同课程，但单轮首版只允许一个课程 scope；跨课程比较必须拆成显式的多 scope 只读用例，首版不实现。

### 4.2 主循环

自研循环，参照四家共同结构：

```python
while turn_count < MAX_TURNS and budget.remaining > 0:
    response = await collect_response(llm.stream(build_request(messages, active_tools)))
    if response.tool_calls:
        results = await execute_tools(response.tool_calls)   # 只读可并发，写入按顺序串行
        messages += [response, *results]
        continue
    exit_reason = response.finish_reason
    break
```

- 每条退出路径记录 `turn_exit_reason`（预算耗尽 / 达到轮次上限 / 正常结束 / 用户中断 / 工具门控拒绝），写入 trace。
- 流式输出：LLM delta 经渠道抽象层分发——Web 走 SSE，IM 渠道聚合成整条消息后发送。
- 普通讲解和规划是主循环默认能力，不需要先激活 skill。只有练习或 Wiki 维护才通过 `use_skill` 加载专项规程；图片在进入主循环前已由附件处理层转换为 `VisionTranscriptionV1`，主 Agent 按常驻规则完成确认和点评。

### 4.3 Tutor 默认行为

Tutor 不是独立角色或 skill，而是系统提示词中常驻的课程问答合约：

1. 回答课程事实、定义、公式或教材观点前，必须先调用 `rag_search` 或读取已有引用的 Wiki 页。
2. 教材证据必须显示文档名和页码；引用不支持结论时不得强行使用。
3. 未找到证据时明确说明。可继续提供通用知识，但必须标注“以下不是当前教材结论”。
4. 只有用户展示了可判定的理解或作答信号时，才调用 `emit_evidence`；普通阅读、礼貌性回复不产生掌握度事件。

### 4.4 上下文组装（读时投影）

每轮请求的上下文由组装器现场构建，分五段（各段的具体提示词写法见第 7 节）：

1. **系统提示**：Tutor 证据合约 + 工具总则 + 可用 skill 摘要列表（只有 name + when_to_use 一句话，不含正文）。
2. **本轮课程上下文**：课程会话使用固定绑定；通用会话注入本轮不可变 `turn_course_context`、解析理由、课程名称、教材列表和可用概念集。这一段是服务端事实，不允许模型修改；未解析时不注入任何课程资料或课程工具。
3. **学习档案注入**：user.md + 当前课程 memory.md + 掌握度概要（由 mastery 表渲染）+ 进行中计划状态 + 相关 wiki 页——按 token 预算截断，弱项优先。
4. **会话历史与结构化产物**：原始消息 append-only 存 SQLite；近期练习题、作答和评分作为 artifact 一并注入。是否正在出题或评分由模型根据用户消息与这些事实判断，不引入硬编码阶段枚举。旧工具输出在读时投影中被裁剪或摘要，原文始终保留。
5. **本轮用户消息**。

分层压缩的设计直接采纳源码调研结论：零成本裁剪先行（工具输出占上下文大头）、LLM 摘要兜底、失败降级，原文永远保留在存储层可回查。

## 5. LLM 接入层

### 5.1 设计目标

Agent 不直接依赖 OpenAI SDK、DashScope SDK 或任一 provider 的响应对象。LLM 接入层统一解决五件事：

1. 把内部消息、图片、工具 Schema 转成 provider 请求。
2. 把文本 delta、工具调用、usage 和结束原因归一化为统一事件。
3. 根据任务所需能力选择文本模型或视觉模型。
4. 统一处理超时、限流、重试、用户取消、流式中断和 provider 错误分类。
5. 为 trace 提供统一的 provider、model、latency、token usage 和 request id。

### 5.2 内部协议

```python
@dataclass(frozen=True)
class ModelCapabilities:
    text: bool = True
    vision: bool = False
    tools: bool = False
    strict_json: bool = False
    reasoning: bool = False
    streaming: bool = True

@dataclass
class LLMRequest:
    messages: list[Message]       # content 可包含 TextPart / ImagePart
    tools: list[ToolSchema]
    response_schema: dict | None
    temperature: float | None      # thinking mode 下必须为 None
    thinking: Literal["enabled", "disabled"]
    reasoning_effort: Literal["high", "max"] | None
    max_output_tokens: int
    trace_context: TraceContext

class LLMClient(Protocol):
    capabilities: ModelCapabilities

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMEvent]: ...
```

`LLMEvent` 只允许 `text_delta / reasoning_delta / tool_call_delta / usage / completed / failed`。Agent 循环只消费这些内部类型，不访问 `choices[0]`、`reasoning_content` 等 provider 特有字段。主 Agent 首版关闭 DeepSeek thinking；若后续评测后为工具链开启，adapter 必须把 `reasoning_content` 作为 provider-private metadata 原样回传，但不得显示给用户、写入通用消息模型、记忆或 trace payload。

### 5.3 模型槽位与能力路由

| 槽位 | 必需能力 | 主要用途 | 可否同一 provider |
| --- | --- | --- | --- |
| `text` | text + tools + streaming | 主 Agent、Tutor、内建规划、practice / wiki skill | 可以 |
| `vision` | vision + structured output | 题干、手写解答、公式与表格转录 | 可以 |
| `judge` | text + structured output | 离线评测 | 可选，建议与生成模型分开 |

路由由业务代码显式指定槽位，不让主模型自己选 provider。进程启动时校验每个已启用功能所需的 capabilities；例如开启 OCR 但未配置 `vision` 槽位时，管理页显示不可用，不在首次图片请求时才报错。

### 5.4 配置模型

```dotenv
TEXT_PROVIDER=deepseek
TEXT_BASE_URL=https://api.deepseek.com
TEXT_API_KEY=...
TEXT_MODEL=deepseek-v4-flash
TEXT_THINKING_DEFAULT=disabled
TEXT_REASONING_EFFORT=high

MODEL_CONTEXT_LIMIT_TOKENS=1000000
AGENT_CONTEXT_WINDOW_TOKENS=131072
AGENT_MAX_INPUT_TOKENS=114688
AGENT_MAX_OUTPUT_TOKENS=8192
AGENT_CONTEXT_RESERVE_TOKENS=8192

VISION_PROVIDER=dashscope
VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_API_KEY=...
VISION_MODEL=qwen-vl-ocr

JUDGE_PROVIDER=...
JUDGE_BASE_URL=...
JUDGE_API_KEY=...
JUDGE_MODEL=...

RAG_EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5
RAG_EMBEDDING_DEVICE=auto
RAG_EMBEDDING_BATCH_SIZE=256
RAG_CHUNK_SIZE=600
RAG_CHUNK_OVERLAP=120
RAG_TOP_K_RESULTS=6

STORAGE_DATA_DIR=./data
SERVER_HOST=127.0.0.1
SERVER_PORT=8000
RESEARCH_SERPAPI_API_KEY=...
APP_LOG_LEVEL=INFO
```

- API Key 只从环境变量或操作系统密钥环读取，不写入 SQLite、Markdown、trace 或前端。
- 2.0 运行时只读取按能力命名的新变量，不再回退 `OPENAI_* / DEFAULT_MODEL*`。若需要迁移 1.0 配置，由一次性迁移命令显式读取旧文件并写成新命名，避免两套变量长期共存。
- 同一把百炼 Key 可以同时配置到 `text` 和 `vision` 槽位，但两个槽位仍然使用各自的 model id 和能力声明。
- 当前中国内地通用 DashScope 域名可继续使用；拿到 Workspace ID 后，生产环境优先切换到北京地域的 workspace 专属域名，并确保 API Key、域名和模型地域一致。

### 5.5 DeepSeek V4 调用策略与 512K 软窗口

截至 2026-07-20，正式模型名是 `deepseek-v4-flash / deepseek-v4-pro`；`deepseek-chat / deepseek-reasoner` 仅是 Flash 非思考/思考模式的兼容别名，并将在 2026-07-24 23:59（北京时间）下线。首版固定 `deepseek-v4-flash`，不再保留两个旧模型名。官方模型支持 1M context，但 API 没有“把窗口改成固定档位”的独立参数；CoursePilot 通过上下文组装器限制发送的 token 数，取 512K 作软窗口，在 1M 上限内留出余量。[DeepSeek 模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)。

当前实现状态（2026-07-21）：`contracts/llm.py` 定义供应商无关的 Tutor 增量流协议（deltas + 终态摘要），`adapters/llm/deepseek.py` 实现流式 Chat Completions（重试仅发生在首个增量之前），`app/bootstrap.py` 是唯一装配点。主链路仅在服务端解析课程且 RAG 返回证据后调用模型；输出增量前的供应商错误通过类型化错误回到 Demo Adapter 并发出 fallback 事件，已输出增量后的中断发 `stream_interrupted` 并保留部分回答。turn 终态由 finally 兜底并在启动时统一恢复，客户端断连或进程崩溃不会遗留 running turn。健康检查只报告配置状态、provider/model 和脱敏后的最近调用状态。

512K 软窗口分配如下，超过任一分区先裁剪该分区，不借用 output/reserve。输出分区受模型输出能力限制，不随窗口放大：

| 分区 | token 上限 | 超限策略 |
| --- | ---: | --- |
| 系统提示 + Tool Schema | 65,536 | Skill 正文按需加载，隐藏不可用工具 |
| 当前用户消息 + OCR 转录 | 49,152 | 附件原文改为引用，保留用户问题 |
| 最近会话历史 | 131,072 | 保留最近轮次，较早内容用会话摘要替代 |
| memory + 计划 + 相关 Wiki | 81,920 | 弱项和当前章节优先 |
| RAG 证据 | 122,880 | 依检索分数裁剪，引用片段不得截断页码 |
| 当前 Skill 正文/私有材料 | 32,768 | 只加载一个前台 Skill |
| 最大模型输出 | 8,192 | 普通回复默认 4,096，复杂讲解最多 8,192 |
| tokenizer 误差与工具循环预留 | 32,768 | 永不填充 |

- 主 Agent、规划、practice 和 wiki 工具链显式传 `thinking.disabled`，保证延迟可控且无需保存隐式推理；离线 judge 可用 `thinking.enabled + reasoning_effort=high`。是否为具体 Skill 开启 thinking 必须先过 A/B eval。
- 非思考模式按任务设置温度：Tutor/评分/Wiki 为 `0.2`，练习题创作为 `0.7`；thinking 模式不发送 `temperature / top_p / presence_penalty / frequency_penalty`，因为官方说明这些参数无效。
- DeepSeek context cache 默认开启。上下文组装保持“稳定系统提示 → 稳定课程信息 → 动态历史/RAG”的顺序，并记录 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`。
- 请求传入内部用户 ID 的 HMAC 作为 `user_id`，用于 provider 侧 KV cache 与调度隔离，不发送邮箱、IM 用户标识等隐私标识。

### 5.6 DeepSeek 与千问 OCR 结论

截至 2026-07-20，DeepSeek 官方列出的 V4 Flash / Pro API 支持 JSON Output 和 Tool Calls，适合主 Agent；但官方同时明确 V4 为 text-only，图片需要由其他视觉模型代理。因此，**单独一个 DeepSeek API 不足以完成 OCR**。另外，`deepseek-chat / deepseek-reasoner` 旧别名将于 2026-07-24 弃用，2.0 不再将该别名写死在代码中。

阿里云百炼的 Qwen-OCR 官方 API 同时支持 OpenAI-compatible 和 DashScope 协议，并提供普通文字、带坐标高精度识别、信息抽取、表格、文档结构与公式 LaTeX 识别。当前使用官方稳定模型 ID `qwen-vl-ocr`；需要锁定行为时可换成文档列出的日期快照（例如 `qwen-vl-ocr-2025-11-20`）。因此首版采用“DeepSeek 文本主模型 + Qwen-OCR 视觉模型”；同一把百炼 Key 可复用，但文本与视觉仍配置不同 model id。

官方能力依据：[DeepSeek 模型与功能](https://api-docs.deepseek.com/quick_start/pricing/)、[DeepSeek V4 发布说明](https://api-docs.deepseek.com/news/news260424/)、[Qwen-OCR API](https://help.aliyun.com/en/model-studio/qwen-vl-ocr-api-reference)。模型 ID 和能力可变，实现以配置与启动能力检查为准。

### 5.7 OCR 调用链路

OCR 不与讲解合并成一次黑盒调用：

```text
图片上传
  -> MIME / 文件大小 / 像素数校验，去除 EXIF
  -> vision 槽位转录
  -> VisionTranscriptionV1 Schema 校验
  -> 关键公式或文字不确定：展示转录并等待用户更正
  -> 转录确定：文本主模型结合 RAG / Wiki 讲解或评分
  -> 确认后才允许 emit_evidence
```

```json
{
  "schema_version": "vision_transcription_v1",
  "plain_text": "...",
  "latex_blocks": [{"latex": "...", "region": [0, 0, 100, 40]}],
  "uncertain_spans": [{"text": "...", "reason": "blurred_or_ambiguous"}],
  "provider": "dashscope",
  "model": "qwen-vl-ocr",
  "needs_confirmation": false
}
```

不强制要求 provider 返回看似精确的 0–1 置信度，因为不同模型的分数不可比。`needs_confirmation` 由适配器根据空转录、关键公式歧义、图片质量与用户修正规则产生。

### 5.8 失败、重试与流式语义

- 仅在还未向用户发出任何文本 delta 时，对 `429 / 5xx / connect timeout` 做最多两次带 jitter 的指数退避重试。
- 已经输出 delta 后中断，不自动重放整轮，避免重复文本和写工具副作用；返回可识别的 `stream_interrupted` 事件。
- 工具调用参数经本地 JSON Schema 再校验，不把 provider 的 strict mode 当成唯一防线。
- 每轮限制 LLM 循环数、工具调用数、输入 token 和输出 token；预算耗尽后输出可理解的终止说明。
- 所有 provider 错误归一为 `auth_error / rate_limited / invalid_request / timeout / upstream_error / cancelled / stream_interrupted`，上层不解析字符串错误消息。

### 5.9 首版运行参数基线

以下值进入 typed settings，启动时校验；代码不得散落魔法数字：

| 参数 | 首版值 | 说明 |
| --- | ---: | --- |
| `AGENT_MAX_LLM_STEPS` | 12 | 单轮最多模型/工具往返次数 |
| `AGENT_MAX_TOOL_CALLS` | 24 | 包含并发只读调用 |
| `AGENT_MAX_PARALLEL_READS` | 4 | 写调用永远串行 |
| `TOOL_RESULT_MAX_BYTES` | 64 KiB | 超出返回摘要和 cursor |
| `LLM_CONNECT_TIMEOUT_SECONDS` | 10 | 建连超时 |
| `LLM_FIRST_TOKEN_TIMEOUT_SECONDS` | 60 | 首 token 超时 |
| `LLM_TOTAL_TIMEOUT_SECONDS` | 180 | 单次 provider 请求总超时 |
| `LLM_MAX_RETRIES` | 2 | 仅输出任何 delta 前重试 |
| `SSE_HEARTBEAT_SECONDS` | 15 | 空闲期间发送注释帧，帮助发现断线 |
| `SESSION_MAX_ACTIVE_TURNS` | 1 | 用户会话和系统会话分别加锁 |
| `SQLITE_BUSY_TIMEOUT_MS` | 5000 | 短写事务，超时返回 typed error |
| `BACKGROUND_JOB_WORKERS` | 1 | 教材索引与 Wiki 本地后台线程数 |
| `BACKGROUND_JOB_QUEUE_CAPACITY` | 8 | 持久化任务进入本地执行器的有界容量 |
| `SCHEDULER_TICK_SECONDS` | 60 | 单 tick 扫描 due items |
| `APPROVAL_TIMEOUT_SECONDS` | 120 | 超时、断连或进程退出均视为 deny |
| `SKILL_ARCHIVE_MAX_BYTES` | 2 MiB | 用户上传 Skill 压缩包上限 |
| `MATERIAL_MAX_BYTES` | 100 MiB | 单本教材上限 |
| `MATERIAL_MAX_PAGES` | 1500 | 超出要求拆分教材 |
| `ATTACHMENT_MAX_BYTES` | 10 MiB | 首版图片上限，低于 provider 极限 |
| `ATTACHMENT_MAX_PIXELS` | 12 MP | 超出先缩放，保留长宽比 |
| `REPLAN_CONSECUTIVE_INCORRECT` | 2 | 只生成重排建议，不直接写计划 |
| `REPLAN_PROGRESS_DEVIATION_DAYS` | 2 | 计划落后达到该值时生成建议 |
| `CHANNEL_DAILY_PUSH_LIMIT` | 3 | 每用户默认上限，可在前端调低 |
| `CHANNEL_QUIET_HOURS` | `22:00-08:00` | 按用户时区；紧急推送也不越过 |

RAG 的 `RAG_CHUNK_SIZE=600 / RAG_CHUNK_OVERLAP=120 / RAG_TOP_K_RESULTS=6` 沿用 1.0 baseline，2.0 首轮不同时改检索算法和 Agent 架构；只有回归评测证明收益后才调整。

### 5.10 接入层验收

1. 每个 adapter 都通过同一套 contract tests：普通文本、SSE 分块、工具调用、JSON Schema、usage、取消与错误分类。
2. vision adapter 额外使用固定的印刷文字、手写公式、模糊图片和表格样本集。
3. 上线前必须用真实课程样本测量字符准确率、公式编辑距离和关键步骤漏识率；不仅凭通用 OCR demo 决定可用性。

## 6. Skill 与 Subagent 体系

明确二分（Hermes 与 Claude Code 的共同结论）：

**Skill = 同上下文的专项操作规程。** Tutor、规划与图片点评都是系统提示词中的默认能力，不做成 skill。系统内置只保留两个专项 skill：`practice`、`wiki_curator`；用户还可以导入自己的纯提示词 Skill。

- 内置 skill 位于 `skills/builtin/<name>/SKILL.md`；用户 skill 位于 `data/skills/<skill_id>/<version>/SKILL.md`（编写规范见第 7 节）。
- **两段式注入**（照搬 Claude Code / opencode）：系统提示只放 skill 摘要列表；模型调用 `use_skill` 工具时，SKILL.md 正文才注入对话。能力说明书不常驻上下文。
- skill 声明 `allowed_tools`，激活期间工具集收窄到声明范围——这是权限门控的主要形式。
- `use_skill` 只加载当轮所需的操作规程，不创建独立 Agent，也不引入持久化阶段状态。一轮只有一个前台 skill，但该 skill 可在同一循环内调用多个受控工具。

**Subagent = 独立上下文的一次性任务执行器。** 仅两个场景使用：

- Deep Research 补料（联网检索大量噪音内容，不应污染主对话）；
- LLM-as-judge 离线评测（独立于用户会话运行）。

实现：spawn 一个新的循环实例，全新消息历史、受限工具集、禁止递归派发，结果取最后一条 assistant 文本返回。不做 worktree 隔离、后台常驻、fork 继承这类重型机制。

练习不做成 subagent 的理由：出题依据、用户作答、评分标准和讲评都需要留在主对话里被用户看到和追问。`practice` skill 根据当前用户消息、最近的练习 artifact 与是否已存在评分 artifact，自主判断本轮是出题、评分、讲评还是生成变式题；服务端不维护 `AWAITING_ANSWER / GRADING` 等硬状态枚举。

### 6.1 用户 Skill 导入

- Web 管理页支持上传单个 `SKILL.md` 或 zip。导入范围可选“全部课程”或某一课程；导入后默认关闭，用户预览说明和请求工具后再启用。
- 首版只接受 UTF-8 的 `.md / .txt / .json` 参考文件，最多 20 个文件、解压后总计 2 MiB；拒绝脚本、二进制、符号链接、绝对路径和 `..` 路径穿越。用户 Skill 不能执行代码、安装依赖或注册新工具。
- frontmatter 必须含 `name / description / when_to_use / allowed_tools`。`allowed_tools` 只是请求，最终权限为“请求集合 ∩ 用户可用工具 ∩ 全局 policy”；Skill 永远不能自行扩权。若请求集合包含被禁止或不存在的工具，版本可作为禁用草稿导入，但状态为 `permission_denied`，用户必须上传修正后的新版本才能启用，不能静默降权后运行。
- 导入时完成 schema、大小、重名与危险内容静态检查，生成不可变 `skill_version` 和内容 hash。更新创建新版本，正在进行的 turn 继续使用旧版本。
- `use_skill` 只看到当前课程已启用的摘要。用户 Skill 与教材内容都视为不可信指令，不能覆盖系统提示、课程边界、Tutor 证据合约或工具 policy。

当前实现只接受单个 `SKILL.md`（prompt-only，≤64 KiB），导入范围是全局而不区分课程；可授予的工具是一份白名单——读工具加练习相关的 artifact 与 `emit_evidence`，`memory_patch`、`plan_update`、`use_skill` 一律不授予。权限不足按上面的规则导入为 `permission_denied` 且不可启用。同名重新导入原地覆盖正文，不保证正在进行的 turn 继续用旧版本；zip、参考文件与多版本并存都还没做。

## 7. 提示词体系与 Skill 编写规范

提示词是这个系统的核心工程资产，与代码同等对待：全部入 git、可 diff、trace 记录版本、judge 评分能归因到具体版本。

### 7.1 提示词分层

| 层 | 内容 | 何时进入上下文 | 维护者 |
| --- | --- | --- | --- |
| 系统提示骨架 | 身份、Tutor 证据合约、规划与图片处理规则、工具总则 | 每轮，静态 | 开发者 |
| Skill 摘要列表 | 各 skill 的 name + when_to_use | 每轮，动态生成 | 由 SKILL.md frontmatter 派生 |
| 学习档案注入 | user.md、memory.md、掌握度概要、计划状态 | 每轮，动态组装 | Agent + 状态层 |
| SKILL.md 正文 | 单个能力的完整操作规程 | 调用 use_skill 时 | 开发者 |
| 嵌入式微提示词 | 归因指令、结构化输出指令 | 随所属 skill | 开发者 |

系统提示骨架的行为准则（初版）：引用教材必须带页码；证据不足时明说而非编造；通用讲解与规划直接执行而不加载 skill；规划必须通过 `plan_read / plan_update` 读写结构化计划；图片必须先展示转录，不确定内容经确认后才能点评和记证据；一轮只激活一个前台 skill；学习信号（答对/答错/可验证的自述）出现时才调用 `emit_evidence`。

### 7.2 SKILL.md 格式

```markdown
---
name: practice
description: 组织完整练习过程，包括出题、评分、讲评和变式题
when_to_use: 用户想练习、提交了对最近练习的作答、要求讲评错题，或每日小测触发时
allowed_tools: [rag_search, wiki_read, concept_search, archive_query, skill_resource_read, artifact_read, artifact_append, emit_evidence]
---
（正文：操作规程）
```

正文统一按五段结构写：**目标 → 步骤 → 输出格式 → 边界（明确不该做什么）→ 一个精简示例**。正文控制在 500–1500 字；更长的参考材料（如评分细则表）放 Skill 同目录附属文件，正文里给出相对路径，激活后由受限 `skill_resource_read` 按需读取——该工具只能读取当前 Skill 不可变版本内已校验的文件，不能访问任意路径。

`when_to_use` 是路由准确率的决定因素，写法要求：描述**触发场景**（用户会说什么话、什么系统事件发生），不描述能力本身。反例："出题技能，可以生成各种题型"；正例：如上 frontmatter 所示。每个 skill 的 when_to_use 需与冒烟集中的路由用例一一对应。

### 7.3 内建能力与两个核心 skill

| 类型 | 能力 | 关键规程 |
| --- | --- | --- |
| 内建 | Tutor | 回答课程事实前先取证据；引用教材页码；证据不足时明确区分教材结论与通用知识 |
| 内建 | 规划 | 用户要求制定或调整计划时直接调用 `plan_read / plan_update`；输出严格遵循 `plan_v1`（里程碑 → 每日条目）；重排只改未来条目，每条关联 `concept_id`；日期、版本和历史保护由计划服务再次校验 |
| 内建 | 图片点评 | 附件处理层先生成 `VisionTranscriptionV1`；主 Agent 先展示完整转录并标注不确定处，待用户确认后再点评；图片是练习作答时激活 `practice`，未确认内容不得触发 `emit_evidence` |
| Skill | `practice` 练习 | 根据用户消息、会话历史和最近 artifact 自主判断出题/评分/讲评/变式题；不使用阶段状态机；出题前取教材证据和弱项；答案与 rubric 可写入模型私有 artifact，用户提交前不得展示；评分后再产生概念证据事件 |
| Skill | `wiki_curator` 维护 | 增量更新而非重写整页；用户手工编辑的段落不覆盖；掌握度区块是状态层渲染的，禁止手写；每次写入附来源事件 id |

### 7.4 Practice Skill 数据约定（非代码状态机）

服务端只提供通用 `artifact_read / artifact_append`，硬校验 envelope：`artifact_id / course_id / session_id / kind / visibility / payload / created_at`。`payload` 由 `practice` SKILL.md 约定，平台不为“出题中、等待回答、评分中”定义枚举，也不要求 PracticeArtifactV1/Pydantic 模型。

Skill 应在需要跨轮保存时记录这些事实：稳定的 `practice_id`、题目、教材引用、模型私有的 answer key/rubric、用户原文、评分和概念归因。`visibility=model_private` 的内容不会进入前端 serializer，只在该 Skill 激活后按需读取。多个练习无法从 `practice_id`、引用和时间近邻唯一判断时，模型必须询问用户。

### 7.5 结构化输出与归因微提示词

- 需要产生确定性副作用的场景（证据事件、计划修改）走 tool call 参数 schema；练习题、评分文本和讲评格式由 practice Skill 管理，不做服务端硬 schema。校验失败自动带错误信息重试一次，再失败则放弃本次写入并记 trace。
- **归因微提示词**（嵌在 Tutor 骨架、图片点评规则与 practice 规程里）："概念必须从下面提供的概念列表中选择，列表外的概念不得使用；无法归因时返回 `unattributed` 而不是猜测。"概念列表由 SQLite `concepts` 表注入——这是挡住幻觉概念污染档案的第一道闸，schema 校验是第二道。

### 7.6 提示词迭代闭环

trace 的每个 skill span 记录 SKILL.md 的 git hash → judge 抽检评分按 hash 聚合 → 修改提示词后对比新旧 hash 的评分与路由准确率。提示词调优从"凭感觉改"变成有版本、有指标的常规迭代。

### 7.7 规程执行的服务端保障

提示词能表达要求，不能保证执行。practice 的每一步漏掉都会让闭环断链（作答不进档案、题目没落盘导致下轮无法批改），因此规程要求的副作用由服务端校验，不只靠 SKILL.md 约束。三层保障各自解决一类失效：

| 层 | 触发条件 | 作用 |
| --- | --- | --- |
| 规则预路由 | 用户消息命中明确的练题意图，或本会话存在尚未批改的练习 | 直接注入规程，不依赖模型主动 `use_skill`；加载后仍由规程判断本轮该出题还是评分 |
| 规程校验补救 | 批改后归因数少于题目数，或出题后没有写 artifact | 追加一轮提醒（带"共 N 道题、已归因 M 道"），只提醒一次 |
| 状态闭合 | 本轮产生了证据但模型没写 `practice_result` | 服务端补写，避免练习永远停在"未批改"、后续每轮都被当成作答重复归因 |

工具轮次上限在 skill 激活后放宽（主 Agent 6 轮 → skill 12 轮）：一次完整评分要读产物、查概念目录、逐题归因，6 轮不够；预算耗尽时供应商可能把 tool call 当普通文本吐出，因此最终回答还要清洗 provider 内部标记。

这些保障的必要性有实测支撑：只靠提示词时冒烟用例通过率约 2/3，逐层补齐后到 8/9。剩余不稳定项（变式题落 artifact）在不同运行间摆动，作为已知的可靠性上限记录在案，不做第四层侵入式兜底。

## 8. 记忆与 Wiki 体系

定性记忆采用主流的 markdown 文件方案，与定量状态严格分工：

```
data/
├─ coursepilot.db                 # 定量：事件流、会话、计划、调度
├─ user.md                        # 全局用户画像（跨课程）
├─ skills/<skill_id>/<version>/   # 用户导入的只读 prompt/reference 文件
├─ courses/
│  └─ <course>/
│     ├─ memory.md                # 该课程的情景记忆
│     ├─ wiki/*.md                # 课程 wiki
│     └─ index/                   # FAISS + BM25 索引
└─ traces/*.jsonl
```

- **user.md**：学习习惯、偏好（讲解详略、语言风格）、长期目标。跨课程生效，每轮注入。
- **courses/\<course\>/memory.md**：课程级情景记忆——学到哪一章、遗留问题、和用户的约定（"下次从习题 5.3 开始"）。仅当前课程注入，"有记忆的开场"直接由它驱动。
- **维护方式**：会话结束或发生重要节点时，Agent 通过 `memory_patch` 工具增量更新受管区块；文件与 wiki 同属一个 git 仓库，每次更新即 commit，可 diff 可回滚。
- **分工红线**：掌握度数值、错题记录、复习排期永远不写入 markdown（memory.md 里可以写"链式法则还没掌握"这类叙述，但判断依据在事件流里）。SQLite 可以存会话原文和练习 artifact，但它们不作为画像叙事的可编辑真源。查询定量状态用 `archive_query`，读取记忆直接随上下文注入。

这一设计吸收了 Hermes（MEMORY.md/USER.md 文件记忆）的形态，但用两条改进规避其缺陷：git 版本化解决"覆写不可审计"，定量/定性分工解决"LLM 覆写污染关键数值"。

### 8.1 概念目录与可选 Wiki 构建

概念目录是证据归因的基础，不等于 Wiki 功能。每次教材完成 RAG 索引后都运行一次轻量、可重放的概念目录任务：

1. 从目录、标题层级、粗体和索引页提取概念候选。
2. 合并同义词，产生稳定 `concept_id`、标准名称、别名、章节与教材引用。
3. 写入 SQLite `concepts / concept_aliases`，不自动生成 Wiki 页面。
4. 新增教材时用新候选与旧概念做 diff；默认不改变已有 `concept_id`。合并或拆分概念必须经管理页确认。

课程字段 `wiki_enabled` 默认 `false`。教材上传和 RAG 建索引不受影响；用户在课程设置打开 Wiki 后，仍需对某本教材点击“解析到 Wiki”，才创建独立 `wiki_build_job`。任务先展示预计页数/概念数，后台生成草稿并逐页提交；失败、取消或重试都不回滚已完成的 RAG 索引。

`emit_evidence` 优先接受 `concepts` 表内的 ID。无法归因时写入 `concept_id=NULL / attribution_status=unattributed / topic_hint`，不更新掌握度；管理页按高频 topic 聚合，用户补录或映射概念后写一条 re-attribution 事件。Wiki 页只是概念的人类可读投影，不反过来充当概念 ID 真源。

### 8.2 Wiki 写入规则

- `wiki_curator` 只在获得新的课程内容事实、经用户确认的纠错，或用户明确要求整理时写 Wiki。普通提问、单次答错和掌握度变化不改写正文。
- 页面 frontmatter 保存 `concept_id / source_refs / schema_version`。Agent 管理区块使用显式 marker，用户手工区块不被自动覆盖。
- 写入前传入 `expected_git_head`；如果用户在 Agent 生成期间已修改页面，工具返回 `conflict` 并要求重读，不强制覆盖。
- 掌握度、复习日期和错题数量在请求 Wiki 页时由服务端动态渲染，不写入 Markdown 和 git 历史。
- `wiki_enabled=false` 时隐藏 `wiki_read / wiki_patch / wiki_curator`，Tutor 只使用 RAG；关闭开关不删除既有页面，重新开启后继续使用。

## 9. 工具系统

2.0 的工具系统重新实现，不以 1.0 ToolHub 代码为地基。1.0 只提供一份回归要求：课程隔离、预算、幂等、错误分类和审计不能丢。

设计参考的不是某个 Agent 的具体工具名，而是三类已经收敛的机制：Claude Code 将内建工具、Skill、MCP 共用同一套权限规则，并把权限判断与 OS sandbox 分层；Gemini CLI 使用 ToolRegistry 和带 `allow / ask_user / deny` 决策的优先级策略引擎；Codex 将 approval policy、sandbox、运行时额外权限和可见工具能力分开配置。Course Pilot 不需要 Shell 和任意文件读写，但需要同样的“**先决定模型能看到什么，再决定某次调用能不能执行**”。依据：[Claude Code tools](https://code.claude.com/docs/en/tools-reference)、[Claude Code permissions](https://code.claude.com/docs/en/permissions)、[Gemini CLI tools](https://geminicli.com/docs/reference/tools/)、[Gemini CLI policy engine](https://geminicli.com/docs/reference/policy-engine/)、[Codex config schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json)、[Codex app-server approval protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)。

### 9.1 两层门控

工具列表不是 Registry 的完整镜像。每轮先由服务端做确定性投影，再把短列表交给模型：

```text
ToolRegistry
  -> capability health filter       # provider、依赖和 feature flag 是否可用
  -> AgentProfile filter            # main / practice / wiki / research-judge
  -> CoursePolicy filter            # 当前课程和渠道允许什么
  -> ToolProjection                 # 只把最终可见工具 schema 发给模型

model tool call
  -> JSON Schema validate
  -> ToolPolicy.precheck            # 课程边界、skill 与硬性 deny
  -> budget / idempotency / version / diff preflight
  -> ToolPolicy.decide              # allow / confirm / deny
  -> ToolExecutor.execute
  -> ToolResult normalize
  -> ToolAudit append
```

`ToolProjection` 只依赖服务端能力、Agent profile、当前激活的 skill 和渠道策略，不根据“正在出题/正在评分”等硬阶段枚举裁剪工具。被 policy 永久拒绝或依赖未配置的工具不应继续展示给模型，既减少误调用，也节省 tool schema token。

首版注册工具少于 20 个，不实现额外 `tool_search`；若以后接入大量 MCP/插件，再将长尾工具延迟到一次工具检索后加载，避免所有 schema 常驻上下文。

### 9.2 默认工具与扩展工具

| Agent profile | 可见工具 | 说明 |
| --- | --- | --- |
| 主 Agent（无前台 skill） | `rag_search`、`wiki_read`、`concept_search`、`archive_query`、`plan_read`、`memory_patch`、`emit_evidence`、`plan_update`、`use_skill` | 覆盖讲解、规划、档案维护和能力加载；Wiki 关闭时自动移除 Wiki 工具 |
| `practice` profile | `rag_search`、`wiki_read`、`concept_search`、`archive_query`、`skill_resource_read`、`emit_evidence`、`artifact_read`、`artifact_append` | 使用通用 artifact 保存必要事实与私有 answer key；看不到计划与 Wiki 写工具 |
| `wiki_curator` profile | `rag_search`、`wiki_read`、`concept_search`、`wiki_patch` | 对 Agent 管理区块做带 `expected_git_head` 的增量 patch；看不到学习档案、计划和练习写工具 |
| 用户 Skill | 其 frontmatter 请求集合与 policy 的交集，另可使用受限 `skill_resource_read` | 默认关闭；不能获得 Shell、任意文件、数据库、调度、渠道发送或未注册工具 |
| Research subagent | `web_search`、`web_fetch` | 与主会话隔离；结果视为不可信外部数据，必须带来源 |

skill 激活时切换到其完整 profile，而不是在默认工具上做无限并集；退出该轮后恢复主 Agent profile。这与对话阶段无关，只是当轮最小权限。

不向模型暴露通用 Shell、任意文件路径、SQLite、Git、`schedule_job` 或 `send_to_channel`。Git commit、调度 tick 和渠道发送由确定性服务执行。图片点评也不注册模型工具：附件处理器把 `VisionTranscriptionV1` 作为结构化上下文交给主 Agent。

### 9.3 注册合约与调用上下文

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    side_effect: Literal["none", "append_internal", "reversible_update", "external"]
    timeout_ms: int
    max_result_bytes: int
    idempotent: bool
    required_capabilities: frozenset[str]

@dataclass(frozen=True)
class ToolCallContext:
    request_id: str
    session_id: str
    course_id: str
    user_id: str
    channel: str
    trigger: Literal["user", "scheduler", "channel"]
    active_skill: str | None
    budget: ToolBudget
```

- 工具由 `@tool(spec=...)` 显式注册，包内显式 import；重名、schema 非法或 handler 签名不匹配时进程启动失败，不做 AST 扫描。
- `course_id / user_id / session_id / idempotency_key` 由运行时注入，不能出现在模型可填写的 schema 中。模型只能提交业务参数。
- 读工具必须有 `limit / cursor` 或固定上限；超限结果返回摘要和 `next_cursor`，不得把整本教材或完整对话历史塞回上下文。
- 工具 description 只写“何时调用、返回什么、关键限制”，不把完整业务 SOP 重复进每个 schema；长规程属于系统提示或 SKILL.md。

### 9.4 Policy 与确认规则

`ToolPolicy` 使用显式规则而非在 handler 内散落 `if`。规则匹配 `tool name + side_effect + trigger + active_skill + arguments + course boundary`，输出 `allow / confirm / deny` 和稳定 reason code；高优先级 deny 永远不能被低优先级 allow 覆盖。

“用户明确要求”只认当前已持久化用户消息中的具体修改请求或计划页提交的结构化操作；系统 job、掌握度触发和含糊表达一律按推断修改处理并进入 `confirm`。`memory_patch` 是明确例外：仅能修改受管区块、使用 repo 级 Git 锁且可回滚，因此会话结束/重要节点的自动维护直接 `allow`，不弹确认。

| 调用类型 | 默认决策 | 例子 |
| --- | --- | --- |
| 当前课程只读 | `allow` | RAG、Wiki、档案和计划读取 |
| 追加式、可纠错的内部记录 | `allow`，前端显示工具回执 | `emit_evidence`、通用 artifact |
| 受限且可回滚的自动记忆维护 | `allow` | `memory_patch` 仅修改 Agent 管理区块 |
| 用户本轮明确要求的可回滚修改 | `allow`，成功回执必须包含 diff | “把考试计划延后一周”触发的 `plan_update` |
| Agent 自己推断出的计划/Wiki 修改 | `confirm` | 因一次答错而想重排计划；未经明确要求整理 Wiki |
| 外部发送、跨课程、伪造概念 ID、越权 skill | `deny` | 主 Agent 直接发 IM、写另一课程、提交不存在的非空 `concept_id` |

无法可靠归因时允许提交 `concept_id=null + topic_hint`，事件进入未归因队列且不更新掌握度；这不等于允许模型编造概念 ID。

`confirm` 返回包含工具名、参数摘要、可读 diff、影响范围和过期时间的 approval artifact。运行时在内存中等待最多 120 秒，Web / IM 渠道提交审批后 resolve future 并继续同一 turn；连接断开、超时或进程重启均视为 deny，不持久化 Agent checkpoint。确认只用于少数副作用场景。

### 9.5 写入、结果与并发合约

- `plan_update` 必须携带 `expected_plan_version`，先生成结构化 diff，再校验 `plan_v1`、日期、概念 ID 和“历史条目不可修改”；成功后只提交新版本，调度 tick 会自然读取当前有效的未来条目，不维护逐条 job。
- `wiki_patch` 必须携带 `expected_git_head` 和区块级 operation；`memory_patch` 只能修改受管 section。冲突返回 `version_conflict`，要求重读后重算，不做 last-write-wins。
- 写工具的幂等键由服务端计算为 `request_id + tool_name + canonical_args_hash`。重试命中已成功审计记录时返回原 `effects`，不重复写入。
- 同一轮 `side_effect=none` 的读工具可并发；写工具按模型输出顺序串行。SQLite 资源按 `course_id + resource_type` 加锁；所有 GitPython add/commit/revert 使用进程内 **repo 级全局锁**，避免不同课程争抢 `.git/index.lock`。取消、超时或流中断后不得自动重放已开始的非幂等写入。
- 所有工具统一返回下列 envelope，模型不从异常字符串猜测执行结果：

```json
{
  "ok": true,
  "data": {},
  "effects": [{"resource": "plan", "operation": "update", "version": 4}],
  "error": null,
  "audit_id": "audit_..."
}
```

失败时 `error` 至少包含 `code / retryable / repair_hint`。工具输出统一标记为 `untrusted_data`；教材、Wiki、OCR 或网页里的命令性文字不能改变系统提示、可见工具或 policy。

### 9.6 可观测性与验收

- 每次调用记录 `requested -> policy_decided -> approval_resolved? -> started -> finished` span，包括 tool/schema 版本、脱敏参数摘要、决策理由、预算变化、幂等命中、耗时、结果大小和 effect 摘要。
- 管理页提供当前 session 的“可见工具”列表以及工具被隐藏的原因；开发环境支持导出单轮 tool transcript，生产环境不记录密钥、answer key 或原始图片签名 URL。
- Registry、Projection、Policy、Executor 各自有 contract tests；至少覆盖跨课程拒绝、未激活/未启用 skill、未知概念、版本冲突、重复 request、读并发、写串行、审批超时和 prompt injection 样本。

## 10. 存储层

SQLite 单文件（`data/coursepilot.db`，WAL），核心表：

| 表 | 性质 | 说明 |
| --- | --- | --- |
| `courses` / `concepts` / `concept_aliases` | 可变 | 课程工作区和稳定概念 ID；课程含 `wiki_enabled=false` 开关 |
| `materials` / `index_jobs` / `wiki_build_jobs` | 可变 / append-oriented | 教材元数据、RAG 索引任务与用户显式触发的 Wiki 解析任务；两类任务互不依赖 |
| `sessions` | 可变 | `scope_mode=general/course`；course 必须有不可变 `course_id`，general 必须为空；`last_resolved_course_id` 仅作列表投影；另含 `kind=user/system` |
| `turn_course_context` | append-only | 每个 turn 唯一的 `resolved/ambiguous/unresolved` 结果、课程、resolver version 与理由；是本轮工具 scope 真源 |
| `turn_requests` | append-oriented | `request_id`、`session_id`、`client_request_id`、`running / completed / failed` 与执行结果；`(session_id, client_request_id)` 唯一 |
| `messages` | append-only | 全量对话原文，含 `complete / interrupted` 状态和 artifact 引用；上下文投影不改此表 |
| `attachments` | append-oriented | 会话附件元数据、内部路径、MIME 与校验结果；必须同时关联 session/course |
| `artifacts` | append-only | 通用 artifact envelope；`kind / visibility / payload` 由 Skill 定义，服务端不维护练习阶段枚举 |
| `evidence_events` | append-only | `concept_id` 可空；空值必须带 `attribution_status=unattributed` 与 `topic_hint`，不进入 mastery 投影 |
| `mastery` | 投影 | 每概念当前 BKT/FSRS 投影、样本数与 `algorithm_version`；可从 evidence_events 重建 |
| `review_queue` | 投影 | FSRS card state 和下次复习时间 |
| `plans` / `plan_items` / `plan_revisions` | 可变 / append-only | 当前计划、条目与每次变更 diff；历史条目不覆盖 |
| `channel_bindings` / `deliveries` | 可变 / append-oriented | 外部用户身份映射与推送去重回执；IM 渠道不保存当前课程 |
| `domain_outbox` | append-oriented | 带版本领域事件、投递状态、attempt 与 next_attempt_at；模块间可靠异步联动，不引入外部消息队列 |
| `tool_audits` | append-only | 工具可见性、policy、审批、幂等与执行审计 |

- **SQLite 事务**：证据事件写入与当前 mastery / review_queue 投影更新在同一事务内完成；投影失败则整体回滚。所有表由显式 schema migration 管理，不在运行时隐式改表。
- **连接与并发**：启动时固定设置 `journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout`。Store 层串行化短写事务，读请求可并发。每个 session 同时只执行一个 turn；用户 session 与课程隐藏 system session 使用独立锁。所有 Git 写操作另受 repo 级全局锁保护。
- **Markdown 层**（user.md、memory.md、wiki）：git 管理，写入工具带 expected head 做乐观并发控制。掌握度在读页时动态渲染，不回写 Wiki Markdown。
- **回滚与纠错**：定量状态不物理截断正式事件流；写入 `correction / supersede` 事件后重放。调试可按任意 seq 做只读投影。Markdown 使用 git revert。
- **Trace**：`data/traces/<date>.jsonl`，每 span 一行。

## 11. 学习档案服务（掌握度实现）

```
Tutor、practice 或经用户确认的图片点评输出结构化归因（概念 + 事件类型）
   → schema 校验（非空概念必须存在；无法归因则进入 unattributed 队列；低置信度 OCR 事件被拒）
   → 写入 evidence_events
   → 同步更新投影：BKT 更新 P（四参数固定默认值），py-fsrs 更新 S 与下次复习时间
   → 发布 EvidenceRecorded 事件；计划服务只消费可解释的学习信号
```

- BKT 参数（P(L0)=0.2, P(T)=0.15, P(G)=0.2, P(S)=0.1 起步）作为带版本的课程配置，后续可按学科调整。每次投影写入 `algorithm_version`，参数变更时从事件流全量重建。
- 练习总分不直接映射 BKT。`practice` 先按 rubric 将每个可判定步骤归因到概念，再产生二值 `attempt_correct / attempt_incorrect`；不可归因的部分不更新 BKT。
- FSRS rating 由确定性规则产生：答错为 `Again`；在提示或重试后答对为 `Hard`；首次独立答对为 `Good`；只有用户明确标记“过于简单”时才为 `Easy`。LLM 不直接输出 rating。
- `follow_up / user_override` 等辅助事件保留在事件流和 UI 证据列表中，默认不进 BKT / FSRS 数值更新。后续如需引入权重，必须新增 algorithm version 并通过 replay 评测。
- 当前展示值定义为 `mastery_score = BKT_posterior * FSRS_retention(now)`，只用于展示和排序，不作为计划重排阈值。UI 同时展示证据数和最近证据日期，不把单一百分比包装成精确测量。
- 少于 3 个可归因客观事件的概念对外呈现“数据不足”。`user_override` 单独展示为用户标记，不伪装成多次客观答题。
- 自动建议重排只由可解释信号触发：同一概念连续 N 次答错、FSRS 到期、计划进度明显偏离；真正写入计划仍遵循 `plan_update` 的用户确认规则。用户也可随时明确要求重排。
- 管理页展示“未归因主题”队列，按 `topic_hint` 聚合频次。用户映射到已有/新增概念后写入 re-attribution 事件并重放投影，原事件不被覆盖。

## 12. 计划与调度

- 计划由主 Agent 的内建规划规则生成（`plan_v1`：里程碑 → 每日条目），通过 `plan_update` 写入 `plans` 表。
- 调度器只注册一个每 60 秒运行的 interval tick，不为条目物化持久化 job。每次查询“当前有效版本中已到期且 `deliveries` 无成功回执”的条目；计划改版无需增删 job。
- 到期条目进入该课程唯一的隐藏 system session，构造系统消息（如“生成今日任务推送”）走 Agent 标准链路。该会话不出现在用户会话列表，并使用独立于用户会话的 turn 锁，因此不会抢占用户正在进行的对话。
- `plan_items` 每次变更生成新 `plan_version`，只改变未来条目；历史条目和已发送回执不修改。
- 投递稳定键是 `source_type + source_id + source_version + channel + scheduled_at`。发送前在 `deliveries` 预登记，成功后写回 channel receipt；重复 tick 返回旧回执。
- **启动补投**：启动后执行同一条到期查询；当天漏发的补投一次，更早条目静默跳过。
- 推送治理在渠道层实现：IM 渠道每日条数上限、免打扰时段、一键退订指令。

当前实现完成了计划的读写与版本化：`plan_update` 在单个写事务里校验 `expected_version`、日期不早于今天、概念 id 属于本课程，整批校验整批拒绝；重写范围限于今天及以后且仍为 `pending` 的条目，已开始的条目保留原状态与 id；每次写入升一版并落一条带 `turn_id` 的 `plan_revisions`。`get_plan` 同时给出弱项与 FSRS 到期概念，让排计划用的是掌握度投影的真实数值。写入的确认规则先用确定性闸门落地——本会话用户明确说过要排或改计划才放行，且判断只取用户键入的原文（图片转录不参与）；§10 要求的 confirm 交互与调度 tick、`deliveries` 都还没做。

## 13. 渠道层

```python
class Channel(Protocol):
    async def send(self, binding: ChannelBinding, message: OutboundMessage) -> DeliveryReceipt
    def on_receive(self, handler) -> None   # 文本/图片统一为 InboundMessage
```

- 首版实现 `WebChannel`（SSE）与 `ImChannel`（长连接）。其他渠道只保留 `Channel` 协议和 adapter 测试夹具，不进入运行时配置。
- `ChannelBinding` 只映射 `provider + external_user_id -> user_id`。外部 ID 经 HMAC 后记 trace，不直接作为内部用户主键；IM 渠道不保存 `active_course_id`。
- 入站消息归一化并取得会话 scope 后才进 Agent；课程会话直接建立本轮 context，通用会话先走 Resolver。Agent 不感知渠道差异；出站消息按渠道能力降级（IM 消息卡片、Web 组件或纯文本）。
- Web 与 IM 渠道可以绑定同一个内部 `user_id`。Web 支持通用/课程会话；IM 渠道对每个用户取得或创建唯一的 `source=im, scope_mode=general` 会话，通过每轮解析确定课程，不按课程拆会话。
- 首版不做语音输入，也不配置 `asr` 槽位；未来新增时通过独立 ASR adapter 接入，不改变 `Channel` 或 Agent 合约。

## 14. HTTP API 与 SSE 合约

2.0 新接口统一使用 `/api/v2`，1.0 `/chat` 与 `/chat/stream` 在迁移期保留为兼容入口。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/courses` | 创建课程工作区 |
| `PATCH` | `/courses/{course_id}` | 修改课程设置，包括默认关闭的 `wiki_enabled` |
| `POST` | `/courses/{course_id}/materials` | 上传教材，返回 `material_id` |
| `POST` | `/courses/{course_id}/index-jobs` | 触发 RAG 索引与概念目录构建 |
| `POST` | `/materials/{material_id}/wiki-jobs` | 用户点击后将指定教材解析到 Wiki；Wiki 未开启时返回 `feature_disabled` |
| `GET` | `/jobs/{job_id}` | 查询索引或 Wiki 构建任务状态与错误摘要 |
| `POST` | `/jobs/{job_id}/cancel` | 请求取消尚未完成的后台任务；已完成步骤保留并明确返回 |
| `POST` | `/sessions` | 创建 `{scope_mode: general}` 或 `{scope_mode: course, course_id}` 会话；默认 general |
| `GET` | `/sessions` | 按 `workspace=general|course:<id>` 可选过滤，返回课程色点与最近解析投影 |
| `GET` | `/sessions/{session_id}/messages` | 读取原始消息与 artifact 引用 |
| `POST` | `/sessions/{session_id}/attachments` | 上传当前会话的图片附件；vision 未配置时返回 `feature_disabled` |
| `POST` | `/sessions/{session_id}/turns` | 发起一轮 Agent 执行，通过 SSE 返回 |
| `POST` | `/courses/{course_id}/knowledge/search` | 知识仓库中对用户明确选定课程做检索验证；通用对话不能绕过 Resolver 调用 |
| `GET` | `/turns/{request_id}` | 查询 turn 最终状态；断线后用于区分完成、中断与进程重启失败 |
| `POST` | `/turns/{request_id}/approvals/{approval_id}` | 确认或拒绝待执行工具；resolve 当前进程内 future，原 SSE 继续 |
| `GET` | `/skills` | 列出内建与用户导入 Skill、版本、作用域和启用状态 |
| `POST` | `/skills/import` | 上传 `SKILL.md` 或受限 zip，完成安全校验后生成不可变版本 |
| `PATCH` | `/skills/{skill_id}` | 设置全局/课程作用域与启用状态 |
| `GET` | `/skills/{skill_id}/export` | 导出用户 Skill 的当前不可变版本；内建 Skill 只读预览 |
| `DELETE` | `/skills/{skill_id}` | 删除用户 Skill；内建 Skill 不允许删除 |

```json
{
  "client_request_id": "uuid-generated-by-client",
  "message": "给我出 3 道链式法则练习",
  "attachment_ids": []
}
```

- `client_request_id` 在同一 session 内唯一。重试相同 ID 返回已有 `request_id`，不新建消息或重复执行写工具。
- turn 请求不接受 `course_id`；服务端从 session 查询。上传的 attachment 也必须属于同一 session。
- 面向前端的 messages / artifact serializer 不返回 `visibility=model_private` 的 payload。practice Skill 可通过通用 `artifact_read` 读取其私有答案或 rubric。
- 用户消息在 LLM 调用前持久化。完整 assistant 消息在 `turn_completed` 时写入；如流式中断，保存已输出部分并标记 `interrupted`。

SSE 事件只使用下列稳定类型：

```text
turn_started      {request_id, session_id, scope_mode}
course_resolution {seq, status, resolved_course_id?, course_name?, course_color?, reason}
text_delta        {seq, text}
reasoning_status  {seq, status}                 # 不发送原始思维链
tool_started      {seq, tool_name, audit_id}
tool_finished     {seq, tool_name, ok, error_code, audit_id}
approval_required {seq, approval_id, tool_name, summary, diff, expires_at}
approval_resolved {seq, approval_id, decision}
citation          {seq, citation_id, document, page, snippet}
artifact          {seq, artifact_type, artifact_id, summary}
usage             {tokens_in, tokens_out}
turn_completed    {message_id, finish_reason}
turn_failed       {error_code, retryable, partial_message_id?}
```

`turn_started` 对通用会话发送 `scope_mode=general` 而不是伪造 `course_id`；随后必须先发送 `course_resolution`。只有 `resolved` 后才出现课程检索、引用或档案工具事件。`seq` 在单 request 内单调递增。出现 `approval_required` 后，原 SSE 保持连接，运行时在内存等待最多 120 秒；审批接口 resolve future 后继续执行。连接断开、审批过期或进程重启均等价于 deny，不序列化 Agent 中间状态。

进程启动时把遗留的 `turn_requests.status=running` 统一终结为 `failed(process_restarted)`；这只是 turn 生命周期清理，不恢复模型循环。前端重连后通过 messages/turn 结果显示失败，用户可重新发起请求。

首版不持久化和重放 SSE delta；断线后前端通过 messages API 取回已保存的完整或 interrupted 消息。错误响应统一为 `{error: {code, message, retryable, request_id}}`。

### 14.1 本地部署安全基线

- FastAPI 默认只监听 `127.0.0.1`，前端与 API 同源托管；CORS 不使用 `* + credentials`。如显式暴露到局域网或服务器，必须开启 Bearer/API token 校验。
- 教材与图片按扩展名、MIME、文件头、大小和页数/像素上限校验，流式写入随机生成的内部文件名，不使用用户文件名拼路径。
- 教材、RAG 片段、Wiki 与 OCR 转录均视为不可信内容；其中的“忽略系统指令”等文本不能改变工具权限或课程边界。ToolPolicy 只信任服务端上下文和注册的 Schema。
- IM 渠道、LLM 和 Git 凭据只从环境变量/密钥环读取；日志过滤 `Authorization`、API Key、bot token 和带签名的图片 URL。

## 15. 前端

React SPA 的视觉系统、组件状态和响应式规范独立维护在 [coursepilot-2.0-frontend-design.md](./coursepilot-2.0-frontend-design.md)。本章只保留后端对前端的产品合约：

1. **对话**：默认通用模式，左栏可进入课程工作区；切换工作区不修改旧会话。会话列表始终可见并以课程稳定色点标记，通用会话顶部区分“本轮解析课程”和永久绑定。
2. **知识仓库**：全局导航名称，进入后必须明确选择课程；默认打开 RAG 资料库，完整展示上传、解析、切块、嵌入、索引和检索验证。Wiki 是同级可选 tab，默认关闭并按教材显式构建。
3. **计划**：日历视图 + 完成情况 + 手动调整入口。
4. **管理**：课程/教材上传、RAG 索引、可选 Wiki 构建、概念合并与未归因队列、用户 Skill 导入/预览/启停、模型槽位能力检查、trace 查看器。OCR 未配置时显示“未启用”，不影响文本学习。

Streamlit 完全退役，不保留。

## 16. 评测与可观测

### 16.1 Trace 合约

Trace 保留 1.0 的可观测能力并增加 LLM / skill / 学习事件 span。主 JSONL 只存索引与摘要：

```text
span_id, parent_id, trace_id, request_id, session_id, course_id
kind(turn|llm|skill|tool|event|delivery), name, status
provider, model, prompt_version, skill_version, tool_audit_id
input_digest, output_digest, input_ref, output_ref
tokens_in, tokens_out, latency_ms, error_code, ts
```

需要离线 judge 的原文、引用和工具结果存在本地 `trace_payloads/`，通过 ref 关联，支持独立保留周期与一键删除。API Key、Authorization header、原始渠道 token 和未脱敏的外部用户 ID 永不进 trace。

### 16.2 评测分层

| 层级 | 验证内容 | 入口 |
| --- | --- | --- |
| Contract | LLM adapter、ToolResult、通用 Artifact envelope、EvidenceEvent Schema | 单元测试，不调真实 API |
| Smoke | 通用课程解析、固定课程隔离、Tutor 取证、practice 出题与评分、skill 路由 | 从 1.0 benchmark 选出的小集合 |
| Full regression | 保留 1.0 全量用例并新增 2.0 学习档案场景 | benchmark + judge + review |
| Replay | 同一证据流在新旧算法下的掌握度与排期变化 | `scripts/replay_mastery.py` |
| Online sample | 忠实度、归因正确性、推送恰当性 | 从 trace payload 抽样，独立 judge |

### 16.3 首版发布门槛

- 课程隔离 contract tests 100% 通过，任一工具不能读写会话外 `course_id`。
- 通用 Artifact envelope / EvidenceEvent Schema 通过率 100%；Skill 自有 payload 不作为服务端练习阶段硬状态。
- Tutor 在有教材证据的测试集上引用覆盖率和引用忠实度不低于 1.0 baseline；无可靠证据时不伪造教材引用。
- `practice` skill 至少覆盖出题、单题作答、多题作答、讲评、变式题和作答对象歧义六类用例。
- 新模型、提示词或 skill 版本上线时，judge 结果按 `model + prompt_version + skill_version` 聚合，不覆盖旧 baseline。

## 17. 代码组织

采用模块化单体。模块只能导入另一模块的 `api.py / models.py / events.py`，具体 repository、service 和 adapter 不对外暴露：

```
course-pilot/
├─ app/                         # composition root；唯一负责装配依赖
│  ├─ bootstrap.py
│  └─ http/                     # FastAPI、SSE、serializer
├─ agent/                       # 主循环、上下文投影、系统提示
├─ modules/
│  ├─ courses/                  # api.py / models.py / events.py / service.py / repository.py
│  ├─ conversations/
│  ├─ skills/
│  ├─ artifacts/
│  ├─ learning/
│  ├─ planning/
│  └─ wiki/
├─ tools/                       # registry / projection / policy / executor / audit
├─ infrastructure/
│  ├─ llm/                      # deepseek / dashscope / fake adapters
│  ├─ rag/                      # 1.0 RAG 通过 port 接入
│  ├─ store/                    # SQLite + migrations
│  ├─ git/
│  ├─ scheduler/                # 单 interval tick
│  └─ channels/im/
├─ skills/builtin/              # practice / wiki_curator
├─ frontend/                    # React SPA
├─ trace/
├─ scripts/
└─ tests/
   ├─ architecture/             # import-linter、模块依赖方向
   ├─ contract/
   ├─ integration/
   └─ regression/
```

跨模块协作只允许三种方式：同步 port 接口、不可变 DTO、带版本 typed event。禁止模块直接读写别人的表、导入内部 service、从 handler 反向调用 HTTP 路由或共享可变全局对象。数据库可以同库，但每张表有唯一 owner；跨 owner 写入必须调用公开接口。CI 用 import-linter 和 architecture tests 固化这些边界，代码 review 必查依赖方向、事务归属、幂等和事件版本。

## 18. 1.0 资产迁移

| 资产 | 处置 |
| --- | --- |
| `rag/` 全部（解析、切块、索引、混合检索、rerank） | 原位复用，先用 adapter 包成 `rag_search`；不在 Agent 重写期间同时更换检索算法 |
| `core/llm/openai_compat.py` | 作为 DeepSeek/OpenAI-compatible adapter 的行为参考，经内部协议隔离后逐步替换 |
| QuizMaster / Grader 的提示词和 Artifact | 合并、简化为 `practice` SKILL.md 与通用 artifact；不迁移练习阶段状态机或专用 Artifact schema |
| ToolHub / RequestContext | 不复用实现；将权限、预算、去重、幂等、错误分类、请求隔离与审计作为新 `tools/` 系统的回归要求 |
| benchmark / judge / review / gold | 整体保留为 full regression，再选小集合作 smoke；新增 trace、skill、归因和 mastery replay 维度 |
| Session 和历史数据 | 提供一次性迁移脚本，为旧 session 补 `course_id`，原文保留为 messages |
| OrchestrationRunner / ExecutionRuntime / 四 Agent 实现 | 新 Agent 主循环逐步接管；迁移期用特性开关切换，新链路达到基线后再删旧实现 |
| Streamlit | React 管理和对话页达到对等能力后退役 |

## 19. 风险与开放问题

- **skill 触发准确率**：单 Agent 靠 `when_to_use` 描述路由，若误触发（该练习时去普通讲解），需要迭代 skill 描述。缓解：冒烟集覆盖路由场景 + 7.6 节的版本化迭代闭环。
- **练习对象歧义**：不使用硬状态机后，多份未评分练习可能让用户的简短答案存在归属歧义。缓解：注入近期 artifact、使用稳定 ID；无法唯一确定时询问用户。
- **归因质量**：证据事件的概念归因是 LLM 输出，归因错误会写入错误概念的掌握度。缓解：归因微提示词限定概念列表 + schema 校验挡住幻觉概念 + 未归因队列 + judge 抽检。
- **用户 Skill 的提示注入与权限膨胀**：导入内容可能要求越权调用。缓解：只允许 prompt-only 文件、权限取交集、默认禁用、版本预览；系统提示与 ToolPolicy 优先级不可覆盖。
- **Wiki 构建质量不稳定**：自动生成页面可能重复或错链。缓解：默认关闭、按教材显式触发、独立 job、可预览/回滚，不影响 RAG 主链路。
- **IM 渠道联调**：首版唯一外部渠道，卡片回调、长连接重连和幂等回执仍需真实环境验证；其他渠道不占首版工期。
- **前端从 Streamlit 换 React 的工作量**：四个页面中 wiki 与计划视图是新增工作量的主体，可按"对话 → 管理 → wiki → 计划"顺序分批交付。
- **OCR 不阻塞主线**：视觉槽位未配置时禁用图片入口，文本学习、练习和计划正常工作。后续选型以真实手写公式样本评测为准。

## 20. 建议实施顺序

重做采用长期渐进路线，但每一步都要形成可运行、可回归的竖向切片：

1. **合约地基**：建立 `store migrations`、LLM 内部协议、ToolResult / ToolCallContext、trace 协议和 contract tests。此时 1.0 产品行为不变。
2. **通用/课程会话与概念目录**：实现 courses、sessions、`turn_course_context`、Course Resolver、materials/index jobs/turns 和 React 基础对话页；RAG 仍通过 adapter 调用 1.0 能力。完成标志是持久会话、逐轮解析、流式回复、课程隔离和概念 ID 测试通过。
3. **Super Agent + Tutor**：新主循环、读时上下文、Tutor 证据合约、新工具系统接管 `rag_search`。通过特性开关与 1.0 learn baseline 对比。
4. **Practice skill**：引入通用 artifact 读写与 `practice` SKILL.md，覆盖文本出题、作答、评分、讲评、变式题和对象歧义；不增加阶段状态机，再退役 QuizMaster / Grader 旧链路。
5. **学习档案**：完成 EvidenceEvent、未归因队列、BKT/FSRS 投影与 replay；掌握度只消费已归因事件。
6. **可选 Wiki**：实现课程开关、按教材触发的 `wiki_build_job`、页面预览/回滚和读时掌握度渲染；关闭时不影响 RAG、Tutor 与练习。
7. **主动化与 IM 渠道**：实现内建规划规则、`plan_read / plan_update`、版本化 plan item、单调度 tick、隐藏 system session 与 Web 通知；本地幂等和补投稳定后接 IM 渠道。其他渠道仅保留协议。
8. **OCR（可选）**：实现视觉槽位、attachment API、`VisionTranscriptionV1` 预处理和系统提示中的图片确认规则；未配置时不影响前七步交付。

每个切片只在 contract tests + smoke + 相关 1.0 baseline 通过后默认开启；旧代码在新切片稳定一轮后再删除，不边写边删回退路径。
