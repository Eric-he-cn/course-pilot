# CoursePilot 2.0 架构设计

对应产品设计见 [coursepilot-2.0.md](coursepilot-2.0.md)。本文确定技术选型与系统架构。设计参照了 Claude Code、Codex、opencode、Hermes Agent 四个主流 Agent 产品的源码调研结论。

## 1. 设计原则

1. **单服务进程优先**：主形态是本地部署，一个 Python 服务进程承载后端能力，不引入消息队列、微服务或容器编排。耗时教材解析使用受控的 worker process，不占用 asyncio 事件循环。
2. **证据事件是定量学习状态的唯一事实源**：掌握度、错题与复习排期先落 append-only 事件，当前值是事件投影，可重放、可审计。会话消息、计划、Markdown 内容和调度任务有各自明确的存储语义，不笼统称为事件投影。
3. **LLM 负责判断与内容，关键副作用走确定性代码**：模型生成讲解、题目、评分和计划提案，也自行判断练习处于出题、作答还是讲评阶段；代码只硬校验跨模块 envelope、证据事件、计划日期、权限和幂等，不把教学流程写成状态机。
4. **自研 Agent 循环，不用编排框架**：主循环本身只有几百行，LangGraph 这类框架带来的抽象成本大于收益。四个参考产品全部是自研循环。
5. **上下文按需组装、原始历史永不修改**：发请求时做读时投影（裁剪、摘要），存储层始终保留全量原文。
6. **定性记忆用 Markdown，定量状态用 SQLite**：画像、情景记忆与知识页是人类可读的 markdown 文件，Agent 只改受管区块；掌握度、错题、排期是事件流投影。Markdown 源文件不持久化掌握度数字，读页时现算。
7. **会话 scope 与工具 scope 分离**：会话可为默认 `general` 或固定 `course`。课程会话的 `course_id` 生命周期内不变；通用会话每轮先由服务端 Course Resolver 产生受控 `ResolvedCourseContext`。工具永远只接受运行时注入的解析结果，不接受模型自由填写课程路径。
8. **模块化单体、接口优先**：课程、会话、Skill、知识库、学习档案、计划、渠道各自拥有数据和服务边界；模块只能依赖公开 Protocol/DTO 或类型化领域事件，禁止跨模块导入实现类、直接查对方表或层层回调。

## 2. 技术选型总表

| 领域 | 选型 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.11+ | RAG 资产直接复用；BKT/FSRS 生态（py-fsrs）；多模态调用只是 API 请求 |
| Web 框架 | FastAPI + SSE | 1.0 已验证，异步生态成熟 |
| LLM 接入 | 自有统一协议 + provider adapter | 参考 1.0 `core/llm/openai_compat.py`，但不让 OpenAI SDK 响应结构泄漏到 Agent；文本与视觉模型独立配置 |
| 结构化存储 | SQLite（WAL 模式），单文件 `data/coursepilot.db` | 存放事件流、会话、计划、调度任务；零运维，量级远够 |
| SQLite 访问层 | 标准库 `sqlite3` + typed repository + 显式 migration | 单文件不需要 ORM；阻塞调用统一放入 worker thread，写事务在 Store 层串行化 |
| 记忆/知识页存储 | Markdown 文件（受管区块 marker） | user.md / memory.md / 知识页；人类可读可编辑，Agent 只改 marker 之间的部分 |
| 向量检索 | BGE 向量（sentence-transformers）+ SQLite FTS 词面，cross-encoder 精排，退化时 RRF 融合 | 沿用 1.0 的模型与融合策略；万级 chunk 用 numpy 暴力点积即可，不引入 FAISS |
| 复习排期 | py-fsrs | FSRS 官方 Python 实现，不自研排期算法 |
| BKT | 自实现（约 30 行贝叶斯更新） | 固定参数的经典四参数 BKT 就是几行公式，pyBKT 是为拟合大数据集设计的，用不上 |
| 定时调度 | APScheduler 单一 interval tick（无持久化 job store） | 每分钟扫描到期条目，计划变更不需要增删大量 job；SQLite 中的计划与 delivery 才是真源 |
| IM 渠道 | IM 平台官方 SDK，WebSocket 长连接模式 | 长连接不需要公网回调地址，本地部署可直接跑 |
| 前端 | React + Vite + TypeScript（SPA） | 替换 Streamlit；会话列表、知识库、计划视图需要真实前端；构建产物由 FastAPI 静态托管 |
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
│                                             │            └ 知识页服务 │
│  后台作业执行器              存储：SQLite + Markdown 文件 + JSONL trace │
└──────────────────────────────────────────────────────────────────────┘
```

- IM 渠道的长连接作为 asyncio task 与 FastAPI 同进程运行，随进程启停；首版只接一个 IM，其余渠道只保留 `Channel` 接口。
- 渠道入站消息必须先解析到 `session_id + scope_mode`。Web 默认创建通用会话，也可进入固定课程工作区；IM 渠道每个用户只使用一个通用会话。通用会话在每轮 Agent 执行前解析相关课程，解析不唯一时进入澄清回复，不执行课程工具。
- Scheduler 每分钟执行一次 tick，查询到期且未投递的计划/复习项；它不直接调 LLM，而是把系统消息投进每门课程唯一的隐藏系统会话。系统会话不出现在用户会话列表、使用独立 turn lock，因此不会与用户正在聊天的 session 抢锁。
- 向量点积、markdown 落盘和 sqlite3 调用使用 `asyncio.to_thread`。教材索引与知识页构建是持久化作业：`jobs` 表是真源，内存里的有界线程池（`BACKGROUND_JOB_WORKERS` / `BACKGROUND_JOB_QUEUE_CAPACITY`）只负责调度排队的行，进程重启后按表恢复。任何可能超过 100 ms 的同步调用都不得直接运行在事件循环。
- 服务器化路径：同一进程部署到服务器 + 前端改为纯静态托管，代码不变。

### 3.1 模块边界与依赖方向

```text
app / web / im / scheduler
              ↓
       application use cases
              ↓
courses | sessions | agent | knowledge | learning | planning | memory | notes
              ↓                         ↑
          ports / DTOs / typed domain events
              ↓                         ↑
     SQLite | RAG | Markdown | LLM | OCR adapters
```

- `modules/<feature>` 只暴露 `api.py`（用例接口）、`models.py`（稳定 DTO）和 `events.py`；实现类、repository 与表结构是模块私有。
- 跨模块同步请求通过 Protocol 端口；一对多联动通过带版本的类型化事件，如 `EvidenceRecorded`、`PlanChanged`、`MaterialIndexed`。需要可靠投递的事件与业务变更同事务写入 SQLite outbox，后台 dispatcher 成功后标记完成；handler 失败按退避时间重试并记 trace，不反向调用发布者。纯 UI 通知可以是进程内 best-effort 事件。
- `app/bootstrap.py` 是唯一 composition root，负责注入 adapter；业务模块不得自行构造数据库、LLM 或渠道 client。
- CI 使用 import boundary test（可用 `import-linter`）禁止越层依赖；代码 review 固定检查“是否绕过公开接口、是否直接访问别的模块数据、是否把副作用藏在回调中”。

## 4. Agent 核心

### 4.1 通用/课程会话与 Course Resolver

`sessions.scope_mode` 为 `general | course`。课程会话要求非空 `course_id` 且创建后不可修改；通用会话的 `course_id` 必须为空，但保存最近一次可靠的 `resolved_course_id` 作为列表投影，不把它提升为永久绑定。

每轮先调用确定性的 `CourseResolverPort.resolve(message, session_summary, attachment_refs, candidate_courses)`，优先级为：用户本轮明确课程名/别名 → 附件所属课程 → 课程会话固定绑定 → 通用会话近期可靠解析 → 候选课程检索分数。只在唯一候选超过阈值时产生 `ResolvedCourseContext(course_id, confidence, reasons)`；否则返回 `needs_clarification`。模型可以生成澄清文案，但不能自行构造解析结果。

`ResolvedCourseContext` 是服务端上下文，不是模型参数。`rag_search`、`wiki_index / wiki_read`、`archive_query`、`artifact_read/append` 等工具从调用上下文取课程，JSON Schema 中不暴露 `course_id`。通用模式允许不同轮次解析到不同课程，但单轮首版只允许一个课程 scope；跨课程比较必须拆成显式的多 scope 只读用例，首版不实现。

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
- 普通讲解、规划和读知识页是主循环默认能力，不需要先激活 skill。练习、联网查证、错题复盘、学习卡片和图解才通过 `use_skill` 加载专项规程；图片在进入主循环前已由附件处理层转换为 `VisionTranscriptionV1`，主 Agent 按常驻规则完成确认和点评。

### 4.3 Tutor 默认行为

Tutor 不是独立角色或 skill，而是系统提示词中常驻的课程问答合约：

1. 回答课程事实、定义、公式或教材观点前，必须先取证据：系统每轮已用用户原话做过一次种子检索，不够就再调 `rag_search`，问整体结构或要并起好几节时读知识页。
2. 教材证据必须显示文档名和页码；知识页是转述、没有页码，标引用时按它自己那一类标。引用不支持结论时不得强行使用。
3. 未找到证据时明确说明。可继续提供通用知识，但必须标注“以下不是当前教材结论”。
4. 只有用户展示了可判定的理解或作答信号时，才调用 `emit_evidence`；普通阅读、礼貌性回复不产生掌握度事件。

### 4.4 上下文组装（读时投影）

每轮请求的上下文由组装器现场构建，分五段（各段的具体提示词写法见第 7 节）：

1. **系统提示**：Tutor 证据合约 + 工具总则 + 可用 skill 摘要列表（只有 name + when_to_use 一句话，不含正文）。语言规则前置在最开头——它管全局，编号排到底下会被后面的中文教材证据压住。
2. **本轮课程上下文**：课程会话使用固定绑定；通用会话注入本轮不可变 `turn_course_context`、解析理由、课程名称、教材列表和知识页目录。这一段是服务端事实，不允许模型修改；未解析时不注入任何课程资料或课程工具。
3. **学习档案注入**：user.md + 当前课程 memory.md + 掌握度概要（由 mastery 表渲染）+ 进行中计划状态 + 对话摘要——按 token 预算截断。
4. **会话历史与结构化产物**：原始消息 append-only 存 SQLite；近期练习题、作答和评分作为 artifact 一并注入。是否正在出题或评分由模型根据用户消息与这些事实判断，不引入硬编码阶段枚举。跨轮投影只送 `user / assistant` 两种角色，工具正文不进下一轮上下文。
5. **本轮用户消息**，以及用它做的一次种子检索取回的教材片段与知识页正文。

分层压缩的设计直接采纳源码调研结论：零成本裁剪先行（工具输出占上下文大头）、LLM 摘要兜底、失败降级，原文永远保留在存储层可回查。

检索类工具的正文与对话原文同表，以 `role='tool'` 落在 `messages` 里（名单与判据见 §9.2）。它们不参与读时投影，因此不占每轮的历史预算、不进会话压缩、界面也不画；模型要看早先某轮取回了什么，走 `history_read` 按需读回。工具正文先于预算计算被摘出去——留在里面会让「更早的消息丢了几条」把它们也数进去，报给用户的数字就不是对话轮数了。

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

`LLMEvent` 只允许 `text_delta / reasoning_delta / tool_call_delta / usage / completed / failed`。Agent 循环只消费这些内部类型，不访问 `choices[0]`、`reasoning_content` 等 provider 特有字段。思考/推理内容（各家字段名不同，如 `reasoning_content`）由 adapter 归一成 `reasoning_delta` 事件：可以展示为过程提示，但不进回答正文、通用消息模型、记忆或 trace payload。是否开启思考模式属于厂商私有配置，走 `TEXT_EXTRA_BODY`。

### 5.3 模型槽位与能力路由

| 槽位 | 必需能力 | 主要用途 | 可否同一 provider |
| --- | --- | --- | --- |
| `text` | text + tools + streaming | 主 Agent、Tutor、内建规划、各内置 skill、知识页构建 | 可以 |
| `vision` | vision + structured output | 题干、手写解答、公式与表格转录 | 可以 |
| `judge` | text + structured output | 离线评测 | 可选，建议与生成模型分开 |

路由由业务代码显式指定槽位，不让主模型自己选 provider。进程启动时校验每个已启用功能所需的 capabilities；例如开启 OCR 但未配置 `vision` 槽位时，管理页显示不可用，不在首次图片请求时才报错。

### 5.4 配置模型

完整字段与默认值见仓库根目录的 `.env.example`，那里是唯一来源；这里只写约束。

模型接入认协议不认厂商：任何说 OpenAI Chat Completions 或 Responses 协议的服务都能配，
包括自建的。`TEXT_PROTOCOL` 选哪一条（`chat` 默认打 `/chat/completions`，`responses` 打
`/responses`），多槽位按 `TEXT_PROTOCOL_N` 逐个指定，不填就继承第一个。两条协议在本项目里
语义等价——消息、流式增量、function 工具调用、思考内容、用量，产出的内部事件完全一致，
上层不知道自己接的是哪一条。要求支持流式与 function calling，否则工具循环跑不起来。
`TEXT_PROVIDER` 只是显示用的名字，不参与任何分支判断——写死厂商名做判断会让别家的配置
静默退回本地兜底。

厂商私有的请求字段统一走 `TEXT_EXTRA_BODY`（JSON 对象，原样并入请求体），
适配器本身只发标准字段。覆盖 `messages` / `input` / `stream` 这类协议字段会在构造期报错；
Responses 那条还拦 `instructions`，因为服务端会把它静默插成第一条 system 消息，
顶在每一次请求（含学科分类器）的规则前面。

Chat Completions 那条吸收掉的三个实际差异：

- 输出上限字段名不统一：默认发 `max_tokens`；`TEXT_EXTRA_BODY` 里给了
  `max_completion_tokens`（OpenAI 推理系模型的要求）就不再发 `max_tokens`，两者互斥。
- 默认带 `stream_options: {"include_usage": true}`，否则部分服务流式 usage 恒为 null。
  遇到不认识这个字段的服务，在 `TEXT_EXTRA_BODY` 里把它置 `null` 即整个移除。
- 思考内容的字段名（`reasoning_content` / `reasoning`）都认，归一成同一个内部事件；
  usage 里嵌套的缓存与思考明细（如 `prompt_tokens_details.cached_tokens`）有则拍平记录。

Responses 那条的对应处置：输出上限是 `max_output_tokens`；不发 `stream_options`（这条协议
没有这个字段，用量随收尾事件 `response.completed` 一起给）；用量字段名（`input_tokens` /
`output_tokens` 及其 details）在适配器里换成与另一条协议同一套内部键名；收尾状态
（`status` + `incomplete_details.reason`）映射成同一套 finish_reason 说法；固定发 `store: false`，
不在厂商侧留会话记录。思考深度这条协议下走 `reasoning.effort`，档位表在 `bootstrap.py` 里另有一张。

已知边界：经代理接 Anthropic 思考模型并同时用工具调用时，思考内容的跨轮回传载体
（带签名的 thinking block）超出这两条协议的表达能力，这类链路不承诺兼容。

- `TEXT_*` 四项配齐并把 `COURSEPILOT_ENABLE_REMOTE_LLM` 打到 1 才会调远端，否则走本地兜底 responder。
- `VISION_*` 四项决定图片提问是否可用，未配置时附件上传返回 `feature_disabled`。
- `RESEARCH_SERPAPI_API_KEY` 留空时，network 类工具整体不下发给模型。
- API Key 只从环境变量或操作系统密钥环读取，不写入 SQLite、Markdown、trace 或前端。
  供应商 4xx 的错误说明会带进本地 trace 用于排查，其中的密钥在写入前抹掉。
- 2.0 运行时只读取按能力命名的新变量，不再回退 `OPENAI_* / DEFAULT_MODEL*`。

### 5.5 调用策略与软窗口

上下文不靠模型侧的窗口参数控制——多数服务没有“把窗口改成固定档位”的独立参数。CoursePilot 在组装阶段限制发送量：模型窗口由 `AGENT_MODEL_CONTEXT_WINDOW` 给（默认 1,024,000 token），软窗口默认取它的一半（512,000），各分区配额再按固定比例从软窗口切出来。换一个上下文更短的模型只改这一个配置项，代码不用动。`AGENT_CONTEXT_TOKEN_LIMIT` 与 `AGENT_HISTORY_TOKEN_BUDGET` 留空即按推导值，显式配了就以配的为准，且都不会超过上一层。思考模式的开关属于厂商私有字段，走 `TEXT_EXTRA_BODY`。

token 数是估算的：中日韩文字按 1 字 1 token，其余按 3.5 字符 1 token。不接 tokenizer 库——那类库只对某一家的 BPE 准，而这里接的是任意 OpenAI 兼容服务。系数两侧都取偏保守的一端，实测对 deepseek-v4-flash 高估 1.5~1.6 倍：宁可少留几条历史，也不要低估之后顶爆上游窗口。

算进去的不只是 `messages`。**工具定义**走 `tools=` 参数，每轮照发、一样吃上游窗口，所以它计进系统提示分区的配额，总闸也把它算进总量——它裁不掉，漏算就会以为还有余量。主 Agent 全套 19 个工具估 3561 token，比系统提示本身还大（撤掉 `delegate` 回到 3114，实测口径同样偏高约 1.2 倍），skill 激活或撤掉 `wiki_*` / `web_*` 都会改变它，因此只按这一轮实际下发的那份算。**思考内容**（`reasoning`）在思考模式下要随消息回传，也一起计；厂商收不收它的钱各家不同，宁可高估。界面把工具定义单开一段展示：它比系统提示还大，混进那一行用户就看不出有多少是自己改不动的固定开销。

`contracts/llm.py` 定义供应商无关的增量流协议（deltas + 终态摘要），`adapters/llm/openai_compatible.py` 与 `adapters/llm/responses_api.py` 各实现一条线上协议、产出同一套事件（重试仅发生在首个增量之前），公共部分在 `adapters/llm/http_chat.py`，`app/bootstrap.py` 是唯一装配点。主链路仅在服务端解析课程且 RAG 返回证据后调用模型；输出增量前的供应商错误通过类型化错误回到 Demo Adapter 并发出 fallback 事件，已输出增量后的中断发 `stream_interrupted` 并保留部分回答。turn 终态由 finally 兜底并在启动时统一恢复，客户端断连或进程崩溃不会遗留 running turn。健康检查只报告配置状态、provider/model 和脱敏后的最近调用状态。

软窗口按固定比例切给各分区（`core/settings.py` 的 `CONTEXT_PARTITION_RATIOS`），组装时逐段核对：超出的只裁本段，不借用别的分区，也不动 output/reserve。下表的 token 数是默认软窗口 512,000 下的取值，换窗口时按同样比例缩放。每一次裁剪都随 `context_usage` 事件报到界面上，正文里也留一句说明——静默截断读起来像“资料就这些”。

| 分区 | 占软窗口 | 默认上限 | 超限策略 |
| --- | ---: | ---: | --- |
| 系统提示（静态规则 + 工具定义 + 教材清单 + 能力摘要 + 练习状态） | 12.5% | 64,000 | 依次收教材清单、练习状态、能力摘要；静态规则与工具定义不动 |
| 当前用户消息（含 OCR 转录）与它派生的检索参数 | 9.375% | 48,000 | 从尾部截断，检索参数只用本分区剩下的额度 |
| 最近会话历史 | 25% | 128,000 | 保留最近轮次，较早内容用会话摘要替代 |
| 长期记忆 + 对话摘要 + 知识页目录 | 15.625% | 80,000 | 先减知识页目录（可用 `wiki_index` 补回），再裁摘要，最后才动用户手写的记忆 |
| RAG 证据（种子检索取回的教材与知识页正文） | 23.4375% | 120,000 | 从尾部截断，知识页转述排在教材原文之后先被切 |
| 当前 Skill 正文 | 6.25% | 32,000 | 只加载一个前台 Skill；正文超限从尾部截断 |
| 模型输出与估算误差预留 | 7.8125% | 40,000 | 永不填充 |

工具循环每轮都会往上下文追加内容（`wiki_read` 一轮能拿 10 × 6000 字符），只在组装时核对一次挡不住，所以每轮进模型前还要过一道总闸 `enforce_context_limit`。整轮超出软窗口时按这个顺序往下裁，直到回到限额内：

1. **较早的工具结果**——换成一句说明（协议要求每个 `tool_call` 都有配对的 tool 消息，不能整条删），模型据此知道那份资料要重新取；
2. **较早的历史消息**——它们已由会话摘要代表；
3. **种子检索证据**——从尾部截断；
4. **最近那几条工具结果**——被上游整轮打回比少读几段更糟，所以宁可截也不放行。

系统提示与本轮提问永远不裁：它们是这一轮要办的事本身。只剩这两段仍然超限时如实报出去，不去动它们。

- 思考档位是厂商私有字段，从配置里的 `TEXT_EXTRA_BODY` 推出默认值（`off / adaptive / high / max`），用户可在界面上按轮切换；知识页构建、课程分类这些后台链路沿用默认档，不各自写死。是否为具体 Skill 固定开启 thinking 必须先过 A/B eval。
- thinking 模式不发送 `temperature / top_p / presence_penalty / frequency_penalty`，因为官方说明这些参数无效。
- 上下文组装保持“稳定系统提示 → 稳定课程信息 → 动态历史/RAG”的顺序，让支持 prompt cache 的服务能命中前缀缓存；usage 里的缓存命中字段（如 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`）有则记录。
- 请求传入内部用户 ID 的 HMAC 作为 `user_id`，用于 provider 侧 KV cache 与调度隔离，不发送邮箱、IM 用户标识等隐私标识。

### 5.6 参考配置：一个双槽位的例子

接入层认 OpenAI Chat Completions 与 Responses 两条协议（vision 槽位只走前者）。text 与 vision 是两个独立槽位：主模型自带图片能力就把两个槽位配成同一个服务；主模型是纯文本的，vision 槽位另配一个视觉模型即可，业务代码感知不到差别。

作者自用的配置可作参考：text 槽位接 DeepSeek（支持 Tool Calls 与 JSON Output，满足工具循环的要求，但 text-only），vision 槽位接同样暴露 OpenAI 兼容协议的 Qwen-OCR（`qwen-vl-ocr`，支持公式 LaTeX 与表格识别；需要锁定行为可换成日期快照版本）。接 OpenAI 或其他多模态服务的部署不需要这层拆分，两个槽位填同一份配置就行。

模型 ID 和能力随时间变化，实现以配置与启动期能力检查为准：开启 OCR 却没配 vision 槽位时，管理页显示不可用，不会等到首次图片请求才报错。

### 5.7 OCR 调用链路

OCR 不与讲解合并成一次黑盒调用：

```text
图片上传
  -> MIME / 文件大小 / 像素数校验，去除 EXIF
  -> vision 槽位转录
  -> VisionTranscriptionV1 Schema 校验
  -> 关键公式或文字不确定：展示转录并等待用户更正
  -> 转录确定：文本主模型结合检索证据与知识页讲解或评分
  -> 确认后才允许 emit_evidence
```

```json
{
  "schema_version": "vision_transcription_v1",
  "plain_text": "...",
  "latex_blocks": [{"latex": "...", "region": [0, 0, 100, 40]}],
  "uncertain_spans": [{"text": "...", "reason": "blurred_or_ambiguous"}],
  "provider": "<VISION_PROVIDER>",
  "model": "<VISION_MODEL>",
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
| `BACKGROUND_JOB_WORKERS` | 1 | 教材索引与知识页构建的本地后台线程数 |
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

### 5.10 厂商端联网搜索（`TEXT_SERVER_SEARCH`，默认关）

Responses 协议上，厂商可以在自己那边执行联网搜索：请求的 `tools` 里多一条
`{"type": "web_search"}`，搜索由服务端跑完、结果直接进模型上下文，我们只在事件流里
看到「它搜了什么」。开关默认关，`chat` 协议下这一项被忽略并在启动时报一句；
学科分类器永远不开它。

**为什么默认关（安全取舍）**：本机的 `web_search` / `web_fetch` 走 executor，网页正文
带不可信内容前缀进上下文、URL 进引用表、次数吃工具预算；厂商端这条三样都没有——
网页内容不经过本地防线，次数也闸不住（真机实测厂商忽略 `max_tool_calls`，也忽略工具体上的
`max_uses`；一个问题搜了 12 次、input 34k token）。所以它是一次明确的取舍，不是默认能力。
开着时本地那两个工具照常在册，模型自己选用哪条路。

**引用能力：没有。** 真机实测厂商在 `output_text` 上给的 `annotations` 恒为空数组，
搜索结果也不经过 executor，因此产不出可点开的引用——开着它时，来自网络的结论在回答里
没有编号可查，只有模型自己写进正文的网址。这条是当前结论，厂商哪天给了来源明细再接。

**可观测**：`response.web_search_call.*` 三个状态事件只带 id（用来记这一步跑了多久），
做了什么与成没成在 `response.output_item.done` 的 `web_search_call` 条目上（`action.type` 是
`search` / `open_page` / `find_in_page`）。适配器把它们归一成 `ServerToolCall`，随本轮的
`ChatToolCalls` / `ChatFinal` 一起交给上层，上层按 `origin="provider"` 报成活动与 trace 里的一步，
与我们自己执行的那些分得开。它不占工具预算——预算闸的是本地执行次数，这里没有本地执行。
子任务（`delegate`）那段循环发不出 SSE，它的厂商端调用收集后交回父轮上报，call_id 带 `sub:`
前缀——父子两边的 id 都由厂商生成，不加前缀撞号就会合成一条。

**回传**：厂商端调用要原样发回下一轮的 `input`，服务端据此恢复自己那边的搜索结果；
不回传的话模型在下一轮只剩自己那句「我来搜一下」，会重搜一遍。位置有约束（真机实测）：
它排在这一轮的思考内容之后、正文之前。摆到思考前面，服务端会当成这一轮没回传思考内容而拒收；
摆到 `function_call` 与它的结果之间则配不上对。工具循环、补救轮与子任务三条路都回传。

### 5.11 过场叙述分流（`TEXT_COMMENTARY_TO_REASONING`，默认开）

Responses 协议上，厂商给每个 `message` 条目标了 `phase`：`commentary` 是调工具前的过场叙述
（「我来帮您查一下天气」），`final_answer` 才是回答。适配器把 commentary 那几段改发成思考流
（`ChatReasoning`，field 记 `output_text.commentary`），正文里不留它，trace 与开发者侧栏照常看得到，
也照常回传给厂商。**准确的 phase 只在条目收尾事件上给**（起始条目一律写着 `final_answer`），
所以正文增量先攒着等定性，攒到 200 字还没收尾就按回答发——长答案的首屏不被这条规则拖住，
真机实测过场叙述在 11~84 字之间。不标 `phase` 的服务一个字都不攒，行为与加这一项之前完全一致。

### 5.12 接入层验收

1. 每个 adapter 都通过同一套 contract tests：普通文本、SSE 分块、工具调用、JSON Schema、usage、取消与错误分类。
2. vision adapter 额外使用固定的印刷文字、手写公式、模糊图片和表格样本集。
3. 上线前必须用真实课程样本测量字符准确率、公式编辑距离和关键步骤漏识率；不仅凭通用 OCR demo 决定可用性。

## 6. Skill 与 Subagent 体系

明确二分（Hermes 与 Claude Code 的共同结论）：

**Skill = 同上下文的专项操作规程。** Tutor、规划与图片点评都是系统提示词中的默认能力，不做成 skill。系统内置五个专项 skill：`practice`、`research`、`mistake_review`、`flashcards`、`diagram`；用户还可以导入自己的纯提示词 Skill。知识页构建不是 skill——它是确定性流水线（见 §8.2），模型只被调来写每一页的正文，不决定读哪些原文、写哪些页。

- 内置 skill 位于 `skills/builtin/<name>/SKILL.md`；用户 skill 存在库里，导入时压成单份文本（编写规范见第 7 节）。
- **两段式注入**（照搬 Claude Code / opencode）：系统提示只放 skill 摘要列表；模型调用 `use_skill` 工具时，SKILL.md 正文才注入对话。能力说明书不常驻上下文。
- skill 声明 `allowed_tools`，激活期间工具集收窄到声明范围——这是权限门控的主要形式。
- `use_skill` 只加载当轮所需的操作规程，不创建独立 Agent，也不引入持久化阶段状态。一轮只有一个前台 skill，但该 skill 可在同一循环内调用多个受控工具。

**Subagent = 独立上下文的一次性任务执行器。** 两个场景使用：

- 成规模的调研（横跨好几个来源、来回换关键词才查得清），模型通过 `delegate` 工具派出；
- LLM-as-judge 离线评测（独立于用户会话运行）。

实现是一个新的循环实例：全新消息历史、只读工具集、禁止递归派发，结果取最后一条 assistant 文本返回。不做 worktree 隔离、后台常驻、fork 继承这类重型机制。子任务沿用父轮选中的模型与思考档位，工具轮次上限 4 轮——每一轮都是一次模型调用，这个数字直接乘进一次 `delegate` 的成本。

三条实现约束：

- **摘要不额外调模型。** 子 agent 自己的最后一轮回复就是交回父轮的成果，为「总结一下」再花一次调用不值。父轮上下文只放这份摘要（3000 token 封顶），完整检索记录落 `kind=delegate_findings` 的 artifact，条数与单条长度都有界（`payload` 有 64 KiB 硬上限，而 task 与正文都由模型写、长度没有上界）。回执里不摆 artifact id：主 Agent profile 没有读产物的工具，摆出来只会换来一次白花的调用。
- **最后一轮要明说工具没了。** 只是不下发 `tools` 的话子 agent 并不知道，它会接着写「让我再查一下」然后停住，那段过场话就成了成果。轮次用尽时追加一句话让它用手上的资料收尾。
- **每一步都要续约心跳。** 一个会话同时只能有一个 running turn，60 秒心跳过期后可被抢占；而子任务跑的时候父轮一个 SSE 事件都不发，心跳只在流式增量分支续约就不够了。

练习不做成 subagent 的理由：出题依据、用户作答、评分标准和讲评都需要留在主对话里被用户看到和追问。`practice` skill 根据当前用户消息、最近的练习 artifact 与是否已存在评分 artifact，自主判断本轮是出题、评分、讲评还是生成变式题；服务端不维护 `AWAITING_ANSWER / GRADING` 等硬状态枚举。

### 6.1 用户 Skill 导入

- Web 管理页支持上传单个 `SKILL.md` 或 zip。导入范围可选“全部课程”或某一课程；导入后默认关闭，用户预览说明和请求工具后再启用。
- 首版只接受 UTF-8 的 `.md / .txt / .json` 参考文件，最多 20 个文件、解压后总计 2 MiB；拒绝脚本、二进制、符号链接、绝对路径和 `..` 路径穿越。用户 Skill 不能执行代码、安装依赖或注册新工具。
- frontmatter 必须含 `name / description / when_to_use / allowed_tools`。`allowed_tools` 只是请求，最终权限为“请求集合 ∩ 用户可用工具 ∩ 全局 policy”；Skill 永远不能自行扩权。若请求集合包含被禁止或不存在的工具，版本可作为禁用草稿导入，但状态为 `permission_denied`，用户必须上传修正后的新版本才能启用，不能静默降权后运行。
- 导入时完成 schema、大小、重名与危险内容静态检查，生成不可变 `skill_version` 和内容 hash。更新创建新版本，正在进行的 turn 继续使用旧版本。
- `use_skill` 只看到当前课程已启用的摘要。用户 Skill 与教材内容都视为不可信指令，不能覆盖系统提示、课程边界、Tutor 证据合约或工具 policy。

当前实现接受单个 `SKILL.md`、含它的 zip，或浏览器直接选一个目录（前端把相对路径写进文件名）。导入时把这一份 skill 压成单份文本：`SKILL.md` 打头，其余 UTF-8 文本文件（`.md / .txt / .json / .yaml / .csv`）按相对路径追加在后面并标出出处，合起来仍受 64 KiB 限制。脚本与二进制文件跳过并回报给用户——平台不执行命令，收下只会让人误以为整份都生效了。附带资料随规程一起注入，没有按需读取，上限就是那 64 KiB。

导入范围是全局而不区分课程；可授予的工具是一份白名单——读工具加练习相关的 artifact 与 `emit_evidence`，`memory_patch`、`plan_update`、`use_skill` 一律不授予。权限不足按上面的规则导入为 `permission_denied` 且不可启用。同名重新导入原地覆盖正文，不保证正在进行的 turn 继续用旧版本；多版本并存还没做。

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
when_to_use: 用户想练习或要做题、提交了对最近练习的作答、要求讲评错题或要同考点的变式题，以及每日小测触发时
allowed_tools: [search_materials, list_materials, concept_search, get_archive, emit_evidence, artifact_read, artifact_append, history_read, web_search, web_fetch]
examples: 出三道题考考我 | 我觉得答案是 B | 讲讲我刚才那道题为什么错
---
（正文：操作规程）
```

正文统一按五段结构写：**目标 → 步骤 → 输出格式 → 边界（明确不该做什么）→ 一个精简示例**。正文控制在 500–1500 字。附属参考材料随规程一起注入，没有按需读取：导入时把整份 Skill 压成单份文本（`SKILL.md` 打头，其余 UTF-8 文本文件按相对路径追加），合起来受 64 KiB 限制。

`when_to_use` 是路由准确率的决定因素，写法要求：描述**触发场景**（用户会说什么话、什么系统事件发生），不描述能力本身。反例："出题技能，可以生成各种题型"；正例：如上 frontmatter 所示。每个 skill 的 when_to_use 需与冒烟集中的路由用例一一对应。

### 7.3 内建能力与内置 skill

| 类型 | 能力 | 关键规程 |
| --- | --- | --- |
| 内建 | Tutor | 回答课程事实前先取证据；引用教材页码；证据不足时明确区分教材结论与通用知识 |
| 内建 | 规划 | 用户要求制定或调整计划时直接调用 `plan_read / plan_update`；输出严格遵循 `plan_v1`（里程碑 → 每日条目）；重排只改未来条目，每条关联 `concept_id`；日期、版本和历史保护由计划服务再次校验 |
| 内建 | 图片点评 | 附件处理层先生成 `VisionTranscriptionV1`；主 Agent 先展示完整转录并标注不确定处，待用户确认后再点评；图片是练习作答时激活 `practice`，未确认内容不得触发 `emit_evidence` |
| 内建 | 知识页导航 | 知识页目录常驻系统提示；问整体结构、学习顺序或要并起好几节才答得全时读两到四页，问具体定义、数字、公式时不读（见 §8.4） |
| Skill | `practice` 练习 | 根据用户消息、会话历史和最近 artifact 自主判断出题/评分/讲评/变式题；不使用阶段状态机；出题前取教材证据和弱项；答案与 rubric 可写入模型私有 artifact，用户提交前不得展示；评分后再产生概念证据事件 |
| Skill | `research` 联网查证 | 教材外的内容才联网；网络结论与教材结论分开写，必须给来源链接；成规模的调研用 `delegate` 派给子任务，一件事派一次 |
| Skill | `mistake_review` 错题复盘 | 读学习档案定位弱项与错题，区分概念错与计算错，给针对性讲解 |
| Skill | `flashcards` 学习卡片 | 把教材内容做成可反复看的卡片，用 `note_write` 落成课程笔记 |
| Skill | `diagram` 图解 | 用 mermaid 画流程图、思维导图、时序图讲清结构与流程 |

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

这些保障的必要性有实测支撑：只靠提示词时 9 条冒烟用例通过 6 条，逐层补齐后到 8/9。剩余不稳定项（变式题落 artifact）在不同运行间摆动，作为已知的可靠性上限记录在案，不做第四层侵入式兜底。

## 8. 记忆与知识页体系

定性记忆采用主流的 markdown 文件方案，与定量状态严格分工。每个用户一份工作区目录，隔离靠目录不靠 `WHERE owner_id`：

```
data/users/<user_id>/
├─ coursepilot.db              # 定量：事件流、会话、计划、分块、概念
├─ user.md                     # 全局用户画像（跨课程）
├─ courses/<course>/memory.md  # 该课程的情景记忆
├─ materials/                  # 教材原件，落盘用生成的文件名
├─ notes/<course>/*.md         # Agent 用 note_write 整理的课程笔记
├─ wiki/<course>/*.md          # 课程知识页
└─ traces/*.jsonl
```

- **user.md**：学习习惯、偏好（讲解详略、语言风格）、长期目标。跨课程生效，每轮注入。
- **courses/\<course\>/memory.md**：课程级情景记忆——学到哪一章、遗留问题、和用户的约定（"下次从习题 5.3 开始"）。仅当前课程注入，"有记忆的开场"直接由它驱动。
- **维护方式**：出现值得长期记住的事实时，Agent 通过 `memory_patch` 增量更新受管区块（`<!-- agent:managed:<section> -->` 之间）。marker 以外是用户自己写的，自动更新不碰。用户也可以在界面上整份改写。
- **分工红线**：掌握度数值、错题记录、复习排期永远不写入 markdown（memory.md 里可以写"链式法则还没掌握"这类叙述，但判断依据在事件流里）。SQLite 可以存会话原文和练习 artifact，但它们不作为画像叙事的可编辑真源。查询定量状态用 `archive_query`，读取记忆直接随上下文注入。

这一设计吸收了 Hermes（MEMORY.md/USER.md 文件记忆）的形态，用定量/定性分工规避它"LLM 覆写污染关键数值"的缺陷。markdown 层不引入 git：受管区块 marker 已经把"哪些字归 Agent、哪些字归用户"划清了，为版本历史再拖一个 GitPython 依赖和一把 repo 级锁不划算。

### 8.1 两条流水线与概念目录

教材上传后跑的是两条独立的流水线，它们共享前半段的文本准备：

```text
提取（PDF/Word/PPT/TXT，扫描件走 OCR）→ 切块 → 写 chunks 表
                    ├──→ 检索索引：向量化 + FTS 索引
                    └──→ 目录结构：概念候选 + parent_id / level / ordinal
```

**拆开的理由是变更代价不同。** 向量化属于检索，概念层级属于结构；绑在一起意味着"改一条概念抽取规则要把 813 页教材重新向量化一遍"。切块的产物两边都要用，所以留在共享段。

- **检索索引**是 `jobs` 表里的后台作业（`type='index'`），阶段走 `extracting → chunking → embedding → indexing`，进度上报到界面。
- **目录结构**只读已落库的 `chunks` 正文（有书签的 PDF 直接读文件目录），亚秒级跑完，因此做成同步接口 `POST /materials/{id}/structure`，不进 `jobs` 表——`jobs.type` 的 CHECK 只有 `index` 和 `wiki`，SQLite 改不了 CHECK。
- 上传时两条依次跑完才把作业记成完成：界面靠这一下刷新概念目录，早一步就会显示上一版。结构解析失败只记 warning，不把已完成的检索索引一起拖垮——它随时可以单独重算，检索却要重跑向量化。

#### 概念目录与层级

概念目录是证据归因的 ID 真源，纯规则抽取、不调模型，所以同一份教材每次重建结果相同。

1. 有目录书签的 PDF 用书签（`pdf_outline` → `from_outline`），剥掉章节编号、滤掉前言目录索引这类非概念标题。没有书签就从正文刮候选：标题层级与 `**强调**`、`「」`，剔除跨页页眉、公式碎片和 PDF 提取乱码。**无书签是主路径而不是兜底**——讲义、扫描件与很多真实教材都没有书签，两条路同等对待，质量差距由界面上的"有无目录层级"如实呈现。
2. `concept_id = sha1(course_id + casefold(name))`，同名概念在同一课程里永远是同一个 id，重放与增量索引都不改动它。只差大小写的候选（`Attention` / `attention`）合成一个。
3. 写入 `concepts`，条数上限 `CONCEPT_LIMIT = 500`。截断浅层优先，砍掉的是最细的小节；它们的内容由更粗的上级概念覆盖，不会消失。
4. `parent_id / level / ordinal` 三列记录教材目录的树形结构。`level` 是所得树里的深度而不是书签的原始层级——祖先被过滤掉时两者会差一层，按树深度算父子关系与缩进才对得上。`ordinal` 是教材里的先后，不能靠 rowid：upsert 保留旧行，同一份教材改版重索引后 rowid 顺序还停在上一版，而知识页按目录顺序切段会整段切错。
5. 同名概念只挂一处，留最浅的那处（并列时留最先出现的）。其余位置不建节点，它们的子节点改挂最近的存活祖先，不会凭空多出一层。

**重建目录结构是破坏性操作，必须先预告。** 概念被删会连带删掉它的掌握度与错题记录，所以 `POST /materials/{id}/structure/preview` 先只读地算一遍：新增几个、保留几个、删除几个，删掉的里面有多少挂着掌握度或错题（"删掉不可恢复"），以及这次能不能解析出层级。预告与执行共用同一份候选和同一套判据，报出的数字就是真正会发生的事。抽取为空时重建是空操作，预告也照这个口径说，不把"抽取失败"读成"这本教材的概念都没了"。

`emit_evidence` 只接受 `concepts` 表内的 ID。无法归因时写入 `concept_id=NULL / attribution_status=unattributed / topic_hint`，不更新掌握度；档案页按 topic 聚合展示这一队列。知识页只是概念的人类可读投影，不反过来充当概念 ID 真源。

### 8.2 Course Wiki：按教材目录自底向上全量遍历

课程字段 `wiki_enabled` 默认 `false`，打开后仍需对某本已索引的教材显式点"生成知识页"，创建 `type='wiki'` 的作业。失败、取消或重试都不回滚已完成的检索索引。

**构建路径上没有检索。** 早先的做法是拿概念名去检索 6 条证据来写一页——一个概念在十处讲了也只看得到六条，知识页于是完整继承了 RAG 的召回缺陷。现在按教材自己的目录切成一棵 Section 树，自底向上写：

| 页类型 | 读什么 | 出处 |
| --- | --- | --- |
| 叶子页 | 它那一节页码区间内的**全部原文**（`MAX_EVIDENCE_CHARS = 6000` 字，超了按分片顺序再切一层，不截断） | 教材页码 `[p.12]` |
| 中间页 | 它各个子页**已经写好的正文**，讲清子小节之间的关系 | 子页名，明令不许标页码——它读的不是原文，页码无从核对 |
| 课程首页 `index.md` | 全部顶层页正文；末尾附的页面目录由落盘清单拼出，不过模型，所以列不出不存在的页 | 顶层页名 |

没有可用目录（无书签、或一页页码都取不到）时按分片顺序切成等大的段，段名让模型读完自己起。

**"不漏"是这条改造的全部意义**，三处设计都服务于它：

- 页码区间由目录顺序推出，首个子节点从父节点起算（章节导语在它之前），最后一节以总页数兜底，区间结尾多带一页（书签指的是标题所在页，跨页标题会指到上一页）。目录里页码倒退时把终点夹到起点，那一节至少读得到自己那一页。落不到任何区间的分片（提取不出页号）按 ordinal 就近补给叶子。
- **节点上限只让页变大，不让内容消失。** `WIKI_MAX_NODES = 300` 是跑飞的兜底，不是目标值：有目录时 BFS 截断砍掉的是最深的那批，它们的页码区间被上级页接过去；无目录时把段合并到刚好装得下，而不是丢掉尾巴。教材自己的结构在这条线以下就照它来——把作者分好的小节并成更少的页，只会让每页要概括的原文成倍上涨。成本交给构建前的账单让用户判断，不靠压小上限来省。
- **判据落在分片上，不落在页码上。** 页码粒度太粗：几段各查几页就能凑满整本书，漏的是页里的分片。旧实现在页码判据下是全绿的，实际每份教材只读到了一半分片。回归测试因此断言"每个分片都被某一节读到"，并在真实教材上按 2 / 4 / 7 / 50 几档节点上限各跑一遍。

其余三条硬约束：**只用教材原文**（写不出来就少写一条，不许拿通用知识补）、**增量刷新**（证据指纹 `source_hash` 没变就跳过，省 token 也省得每次生成一个不一样的版本）、**手写区不动**（`HANDWRITTEN_MARKER` 以下归用户，重新生成只换上半部分）。页面 frontmatter 记 `concept_id / material_id / parent_id / level / order / source_hash / prompt_version / source_refs`；掌握度不写进文件，读页时现算才不会过期。

构建前后都要把覆盖率说出来：`GET /materials/{id}/wiki/estimate` 离线跑一次切段，报预计页数、模型调用次数和耗时（实测约 5 秒一页）；作业结束时回一行 `wiki_coverage concepts=… pages=… written=… skipped=… merged=… empty=… pruned=… issues=… outline=…`，界面按字段渲染。静默截断读起来像"这本书就这些"。
`issues` 是体检发现的条数，体检没跑成时这个字段整个不出现——0 是"查过、没问题"的结论，不能拿来顶替。
这一行落在作业记录里，`GET /materials/{id}/wiki/report` 回读最近一次跑完的那份，刷新页面不丢。

**体检**（`GET /courses/{id}/wiki/lint`）是零模型调用的确定性检查，只报不改：报告是接口，改不改由用户决定。
判据分两级，error 是该重建或该修的（正文标的页码不在这页出处里、总览页标了教材页码、带页码的编造出处、
叶子页零出处、出处指向教材里不存在的页），warn 是提示（无页码的文档级标注对不上出处、引了这一页没读过的教材、
没人读的页、空正文、孤儿页、缺证据指纹）。
只查正文不查手写区——用户自己写 `[p.99]` 不是缺陷。规则全在一个纯函数里，页数据与对账用的页集合由服务层查库喂进去。

**配对**（`GET /courses/{id}/wiki/graph`）回答「哪几个来源在讲同一件事」，服务的是多份教材讲同一节的场景。
读时现算，不落盘：证据没变的页不重写，边写进 frontmatter 就再也回填不进去。

**只连跨教材的两页。** 一门课只有一本书时本来就没有「几个来源」，同一本书里的相邻小节是「相关」不是
「同一件事」，它们的关系中间页也已经用自然语言写过。同章的两页必然同教材，这一条把同章的边一并挡在外面。
判据是知识页向量的余弦：互为 6 近邻才算候选，门槛取这门课直系兄弟页相似度的中位数（余弦的绝对值不跨库
可比，每门课自标定；顶层页不算兄弟组，没有兄弟对时退回全部页对的中位数），每页最多 3 条。门槛是相对量，
页彼此都不像时它会降到 0，所以余弦不为正的页对一律不连——那是它下面唯一的绝对线。
一页什么都没连上时保留它分数最高的那个来源：另一本书讲同一节时措辞不同，未必够得着按同章标定的线，
而「几本书都讲了这一节」正是这件功能要兑付的东西。

**只给界面，模型侧一个字都不下发**——把这样一张关系表摆到模型眼前，它会顺着链接把整门课读一遍。
没配嵌入模型时是空表，不做词面兜底：两套排序会给出两种结论。

**手写区编辑**是「用户纠偏」这一环的入口。读一页时 `body` 与 `handwritten` 分成两个字段下发，
界面各自渲染，落盘格式（frontmatter、分隔标记）不上屏；`PUT /courses/{id}/wiki/{concept_id}/handwritten`
只收分隔线以下那一段，生成区与 frontmatter 一个字节都不动，读改写整段落在 store 的锁里。
这门课有排队中或正在跑的构建作业时拒绝写入（409）——构建会把整页重写一遍，交错保存必然丢更新；
整页超过 `MAX_PAGE_BYTES` 返回 413，盘上的内容原样留着。手写区仍然不进体检，模型读到它时带归属标注。

### 8.3 知识页是第三类可引用来源

知识页正文写进 `chunks` 表（`source_kind='wiki'`，另带 `concept_id / concept_name`，无页码），整课替换。挂在 `chunks` 上是为了让删教材、删课程那两条既有的清理链路照样收走它们。

- **一次种子检索同时覆盖两边，名额固定：教材 6 条 + 知识页 2 条**，各按自己那一路排序，不做统一排序也不做路由。不合排的理由：知识页用概括的语言写、提问也常是概括的语言，放进同一个列表比相似度它会占便宜，把教材原文挤出去，结果是照着转述回答。固定名额也意味着调整知识页那一路的阈值不会挤占教材席位。
- 教材那一路的检索必须带 `source_kind='chunk'` 过滤，词面、向量、FTS 三条路都要带。漏一条，知识页构建就会把自己上一轮的输出当成教材证据读回来，分片覆盖立刻掉下去。
- `CitationRegistry` 里三类来源共用一套编号，`kind` 区分：教材按 chunk 去重、知识页按 `concept_id` 去重、网页按 URL 去重。`wiki_read` 读到的页和检索命中的同一页是同一条来源，不编两个号。
- **知识页是转述，没有页码，界面必须让用户一眼看出来。** 教材引用是深色带页码，知识页是灰色标「知识页 · 概念名」，网络是蓝色链接。要教材出处就回 `search_materials` 查原文，用那一次返回的编号。这句话既写在工具描述里，也写在返回正文的头部——只写在描述里的话，模型读完长正文就只记得内容，转头把它当成有页码的教材证据。
- 知识页正文单开一个上下文分段 `context.segment.wiki_evidence` 上报，不并进"教材证据"那一行：把转述算进教材证据会让用户读错那一行。

### 8.4 知识页目录常驻系统提示

知识页只靠模型主动去取时，实测 20 个样本轮次里只有 2 次进过。所以把目录（`concept_id | 概念名`，上限 60 条）直接注入系统提示，摆在教材清单之后，随之给出"什么时候该读知识页"的规则；`wiki_index` 退化成注入上限之外才用的补充工具。

- 位置比措辞更要紧：明说"分成哪几块"的问句改前就有 17/24 会读知识页，而"没明说全貌、但要把好几节并起来才答得全"的那类只有 5/24。前置并写硬后，两类分别到 23/24 和 17/24，单点问答没有过度触发（1/32 → 2/32）。
- 措辞里留了两条约束，都是踩过的坑：目录一摆出来模型想整份读完（单轮 `wiki_read` 26 次、预算 10、后面全是空转），所以明写"挑两到四页"；`concept_id` 摆进提示词会被抄进回答，所以明写"id 只用于调工具，回答里只说概念名"。
- 目录进的是 knowledge 分区，超限时最先减的就是它——少列的页用 `wiki_index` 补得回来。实测 20 页课占 660 token、60 页封顶 1271。
- 课程没开知识页时，目录段与 `wiki_index / wiki_read` 一起撤下，共用 `wiki_entries` 这一个判据，不会各撤各的。推荐读不到的东西，模型会口头答应去读而实际读不到。

## 9. 工具系统

2.0 的工具系统重新实现，不以 1.0 ToolHub 代码为地基。1.0 只提供一份回归要求：课程隔离、预算、幂等、错误分类和审计不能丢。

设计参考的不是某个 Agent 的具体工具名，而是三类已经收敛的机制：Claude Code 将内建工具、Skill、MCP 共用同一套权限规则，并把权限判断与 OS sandbox 分层；Gemini CLI 使用 ToolRegistry 和带 `allow / ask_user / deny` 决策的优先级策略引擎；Codex 将 approval policy、sandbox、运行时额外权限和可见工具能力分开配置。Course Pilot 不需要 Shell 和任意文件读写，但需要同样的“**先决定模型能看到什么，再决定某次调用能不能执行**”。依据：[Claude Code tools](https://code.claude.com/docs/en/tools-reference)、[Claude Code permissions](https://code.claude.com/docs/en/permissions)、[Gemini CLI tools](https://geminicli.com/docs/reference/tools/)、[Gemini CLI policy engine](https://geminicli.com/docs/reference/policy-engine/)、[Codex config schema](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json)、[Codex app-server approval protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)。

### 9.1 两层门控

工具列表不是 Registry 的完整镜像。每轮先由服务端做确定性投影，再把短列表交给模型：

```text
ToolRegistry
  -> capability health filter       # provider、依赖和 feature flag 是否可用
  -> AgentProfile filter            # main / 各内置 skill / 用户导入 skill
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

注册工具二十出头，不实现额外 `tool_search`；若以后接入大量 MCP/插件，再将长尾工具延迟到一次工具检索后加载，避免所有 schema 常驻上下文。这个数字要盯着：主 Agent 那份工具定义已经比系统提示本身还大（见 §5.5）。

### 9.2 默认工具与扩展工具

| Agent profile | 可见工具 | 说明 |
| --- | --- | --- |
| 主 Agent（无前台 skill） | `search_materials`、`list_materials`、`concept_search`、`wiki_index`、`wiki_read`、`get_plan`、`plan_update`、`get_archive`、`history_read`、`note_read`、`note_write`、`memory_patch`、`emit_evidence`、`calculator`、`web_search`、`web_fetch`、`use_skill`、`ask_user`、`delegate` | 覆盖讲解、规划、档案维护、能力加载和派子任务 |
| `practice` profile | `search_materials`、`list_materials`、`concept_search`、`get_archive`、`history_read`、`web_search`、`web_fetch`、`emit_evidence`、`artifact_read`、`artifact_append`（+ 基座工具） | 使用通用 artifact 保存必要事实与私有 answer key；看不到计划与笔记写工具 |
| 其余内置 skill | 各自 frontmatter 声明的集合（+ 基座工具） | `research` 加联网与 `delegate`、`mistake_review` 加档案读、`flashcards` / `diagram` 加笔记读写 |
| 用户 Skill | 其 frontmatter 请求集合与白名单的交集 | 默认关闭；`memory_patch` / `plan_update` / `use_skill` / `delegate` 一律不授予，也不能获得 Shell、任意文件、数据库、调度或未注册工具 |
| 子任务（`delegate` 派出） | `search_materials`、`list_materials`、`concept_search`、`wiki_index`、`wiki_read`、`web_search`、`web_fetch`、`calculator`、`note_read` | 全是只读取证工具，一件写操作都没有；它没有界面、反问不了用户，所以 `ask_user` 也不给。网络结果视为不可信外部数据，必须带来源 |

工具名里 `search_materials / get_plan / get_archive` 分别对应本文其他章节使用的历史名 `rag_search / plan_read / archive_query`。

skill 激活时切换到其完整 profile，而不是在默认工具上做无限并集；退出该轮后恢复主 Agent profile。这与对话阶段无关，只是当轮最小权限。

课程没开知识页时 `wiki_index / wiki_read` 整体不下发，且系统提示里的知识页目录同时撤下。这一摘发生在工具集这一层而不是能力这一层——它们和 `search_materials` 同属 `read_course`，按能力摘会把整档一起摘掉；摘在工具集上，schema 下发与运行期准入读的才是同一份名单。`wiki_read` 的每轮额度给到 10 次：一页正文只有几百字，看全貌就是要连读好几页，这个数是防它把几十页索引一路读完，不是配给。

`delegate` 把一件成规模的调研派给子任务（形态见 §6），参数只有 `task / expect / sources / avoid`——**不给模型 `tools` 参数**，子任务能用哪些工具由服务端定。它单开一档能力 `delegate`：归到 `free` 会让「不花钱的工具」这句话不再成立，而预算判断正靠它；每轮额度 2 次，够「派一件、看完成果再补派一件」。三条约束：

- **子任务与父轮共享同一份额度计数。** 子任务花掉的 `web_search` 等次数算在父轮头上，它不是绕开预算的口子。
- **递归入口在注册期就断死。** 子任务的工具集与 `{delegate, use_skill}` 的交集非空、或它的能力集含 `delegate` 时，`validate_profiles` 报错、进程启动失败——前者让子任务能继续往下派，后者让规程在子循环里再展开一层。
- **没有派子任务的意图时整体不下发**（照 `wiki_*` 的先例摘在工具集这一层），系统提示里推荐它的那一段同时撤下。这道意图闸门比计划写入那道更紧，只认「明说要做一件成规模的调研」：漏放只是这一轮模型自己去查，误放要花用户的钱。判据同样只看用户键入的原话，图片转录不参与。skill 激活后不再摘——声明它的 `research` 本来就是被意图路由进来的，那一步已经是一道闸门。

`history_read` 是只读工具：跨轮历史按 role 投影，工具正文不进下一轮，模型要看早先某轮检索到什么就得回捞。它一次最多回看 5 轮、6000 字符，每轮 3 次额度，只回放当前课程轮次的工具痕迹、落库的工具正文与引用原文，不碰模型私有 artifact（那里存着答案）。摘要来自消息的 `activity` 字段、正文来自同一张表里 `role='tool'` 的行——同一条读回路径的两半。有正文的那一轮不再重贴引用片段：那几段原文正文里已经是全文。

**哪些工具的正文落库**，按四条判据取，缺一条就不落：正文是取回的资料而不是「已保存」这类回执；同样的内容用户在引用面板或资料库里本来就看得到；不读消息表自己（否则历史会自我复制）；内容不随时间变。在册六个：`search_materials`、`concept_search`、`wiki_index`、`wiki_read`、`web_search`、`web_fetch`。`artifact_read` 出局——它会带出 `model_private` 的标准答案；`get_plan` / `get_archive` / `note_read` 出局——每轮重读才是最新的，存下来只会让模型抄到过期版本，`plan_update` 还会因为旧 `expected_version` 撞版本冲突；`list_materials` 出局——文件清单每轮都在系统提示里。单条正文上限 8000 字符，超了截断并在正文里说明。落库失败只记日志不打断对话：最坏是这一段以后回看不到。子任务查到的资料走同一条落库路径，`call_id` 加 `sub:` 前缀错开——父子两边的 id 都由模型生成，撞上会让 `history_read` 把子任务的正文接到父轮某次调用的摘要底下。

`memory_patch` 与 `ask_user` 是基座工具，每份 profile 都补上，不由各 skill 自己声明：整体替换意味着不兜住就会在 skill 激活后消失，而「记下值得长期记住的事」和「把选项摆给用户挑」在任何规程执行期间都可能需要，两者又都不碰课程数据。`ask_user` 只把选项挂到本轮消息上就收住，用户点击等于发一条新的用户消息，不占住当前 turn 等人。这一轮真能写计划（写权限开着、`plan_update` 在册）而反问又是在问排计划的参数时，服务端保证选项里有一条「就按默认排计划」的出口：计划的事后兜底在本轮以 `ask_user` 收住时主动放过（那时逼模型写就是让它自己编日期），没有出口用户就只能把需求重说一遍。出口的措辞必须能被计划写入的意图判据认出来——用户点它等于发一条新消息，模型第二次只说不写还得有人拦；模型自己写的出口常常少了关键词，那种就地换成标准措辞。选择题与和计划无关的澄清不给出口。

不向模型暴露通用 Shell、任意文件路径、SQLite、`schedule_job` 或 `send_to_channel`。知识页构建、调度 tick 和渠道发送由确定性服务执行。图片点评也不注册模型工具：附件处理器把 `VisionTranscriptionV1` 作为结构化上下文交给主 Agent。

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

“用户明确要求”只认当前已持久化用户消息中的具体修改请求或计划页提交的结构化操作；系统 job、掌握度触发和含糊表达一律按推断修改处理并进入 `confirm`。`memory_patch` 是明确例外：仅能修改受管区块、文件层加进程内锁，因此会话结束/重要节点的自动维护直接 `allow`，不弹确认。

| 调用类型 | 默认决策 | 例子 |
| --- | --- | --- |
| 当前课程只读 | `allow` | 检索、知识页、档案和计划读取 |
| 追加式、可纠错的内部记录 | `allow`，前端显示工具回执 | `emit_evidence`、通用 artifact |
| 受限且可回滚的自动记忆维护 | `allow` | `memory_patch` 仅修改 Agent 管理区块 |
| 用户本轮明确要求的可回滚修改 | `allow`，成功回执必须包含 diff | “把考试计划延后一周”触发的 `plan_update` |
| Agent 自己推断出的计划修改 | `confirm` | 因一次答错而想重排计划 |
| 外部发送、跨课程、伪造概念 ID、越权 skill | `deny` | 主 Agent 直接发 IM、写另一课程、提交不存在的非空 `concept_id` |

无法可靠归因时允许提交 `concept_id=null + topic_hint`，事件进入未归因队列且不更新掌握度；这不等于允许模型编造概念 ID。

`confirm` 返回包含工具名、参数摘要、可读 diff、影响范围和过期时间的 approval artifact。运行时在内存中等待最多 120 秒，Web / IM 渠道提交审批后 resolve future 并继续同一 turn；连接断开、超时或进程重启均视为 deny，不持久化 Agent checkpoint。确认只用于少数副作用场景。

### 9.5 写入、结果与并发合约

- `plan_update` 必须携带 `expected_plan_version`，先生成结构化 diff，再校验 `plan_v1`、日期、概念 ID 和“历史条目不可修改”；成功后只提交新版本，调度 tick 会自然读取当前有效的未来条目，不维护逐条 job。
- `memory_patch` 只能修改受管 section，marker 以外的用户手写内容不动。知识页没有模型可调的写工具：正文由 §8.2 的构建流水线整页替换，手写区始终保留。
- 写工具的幂等键由服务端计算为 `request_id + tool_name + canonical_args_hash`。重试命中已成功审计记录时返回原 `effects`，不重复写入。
- 同一轮 `side_effect=none` 的读工具可并发；写工具按模型输出顺序串行。SQLite 资源按 `course_id + resource_type` 加锁。取消、超时或流中断后不得自动重放已开始的非幂等写入。
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

失败时 `error` 至少包含 `code / retryable / repair_hint`。工具输出统一标记为 `untrusted_data`；教材、知识页、OCR 或网页里的命令性文字不能改变系统提示、可见工具或 policy。

### 9.6 可观测性与验收

- 每次调用记录 `requested -> policy_decided -> approval_resolved? -> started -> finished` span，包括 tool/schema 版本、脱敏参数摘要、决策理由、预算变化、幂等命中、耗时、结果大小和 effect 摘要。
- 管理页提供当前 session 的“可见工具”列表以及工具被隐藏的原因；开发环境支持导出单轮 tool transcript，生产环境不记录密钥、answer key 或原始图片签名 URL。
- Registry、Projection、Policy、Executor 各自有 contract tests；至少覆盖跨课程拒绝、未激活/未启用 skill、未知概念、版本冲突、重复 request、读并发、写串行、审批超时和 prompt injection 样本。

## 10. 存储层

SQLite 单文件（`data/users/<user_id>/coursepilot.db`，WAL），核心表：

| 表 | 性质 | 说明 |
| --- | --- | --- |
| `courses` / `concepts` / `concept_aliases` | 可变 | 课程工作区和稳定概念 ID；课程含 `wiki_enabled=false` 开关；概念带 `parent_id / level / ordinal` 三列还原教材目录树 |
| `materials` / `jobs` | 可变 / append-oriented | 教材元数据与后台作业；`jobs.type` 只有 `index`（检索索引）与 `wiki`（知识页构建），目录结构解析是同步接口不入表 |
| `chunks` / `chunks_fts` | 可变 | 教材切块与知识页正文共用一张表，`source_kind` 区分（`chunk` / `wiki`）；FTS 只索引教材原文，知识页页数以十计走 LIKE 兜底 |
| `sessions` | 可变 | `scope_mode=general/course`；course 必须有不可变 `course_id`，general 必须为空；`last_resolved_course_id` 仅作列表投影；另含 `kind=user/system` |
| `turn_course_context` | append-only | 每个 turn 唯一的 `resolved/ambiguous/unresolved` 结果、课程、resolver version 与理由；是本轮工具 scope 真源 |
| `turn_requests` | append-oriented | `request_id`、`session_id`、`client_request_id`、`running / completed / failed` 与执行结果；`(session_id, client_request_id)` 唯一 |
| `messages` | append-only | 全量对话原文，含 `complete / interrupted` 状态和 artifact 引用；上下文投影不改此表。`role` 认 `user / assistant / system / tool`，`tool` 行是落库的检索工具正文（§9.2），不投影进上下文、不进压缩、也不返回给前端 |
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
- **连接与并发**：启动时固定设置 `journal_mode=WAL`、`foreign_keys=ON`、`busy_timeout`。Store 层串行化短写事务，读请求可并发。每个 session 同时只执行一个 turn；用户 session 与课程隐藏 system session 使用独立锁。
- **迁移**：编号迁移之外，增删列走按 `PRAGMA` 对账的 `ADDED_COLUMNS`。`ALTER` 没有 `IF EXISTS`，写成编号迁移的话同一批里后面一句失败会让版本号没落库，下次重跑必撞 duplicate column，工作区就再也起不来；按现存结构对账则重复执行天然安全。
- **放宽 CHECK**：SQLite 改不了 CHECK，只能整表重建，走 `WIDENED_CHECKS`——按 `sqlite_master` 里的现存 DDL 对账，已含新 CHECK 就跳过，不含就把旧 CHECK 子句替换掉重建，整个包在一个事务里，同样重复执行天然安全。DDL 与预期对不上时中止而不是猜着改。重建按官方顺序：建暂存表 → 搬数据 → 删旧表 → **把暂存表改成正名** → 重建索引 → `PRAGMA foreign_key_check` 复核。改名必须落在新表上，反过来先给旧表改名会毁掉别的表指过来的外键（见 `Docs/development.md` 的「踩过的坑」）。
- **Markdown 层**（user.md、memory.md、notes、wiki）：落盘文件，写入前校验落点仍在本课程目录内且不是符号链接。Agent 只改受管区块或整页替换，用户手写部分不动。掌握度在读页时动态渲染，不回写 markdown。
- **回滚与纠错**：定量状态不物理截断正式事件流；写入 `correction / supersede` 事件后重放。调试可按任意 seq 做只读投影。
- **Trace**：`data/users/<user_id>/traces/<date>.jsonl`，每 span 一行。

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
- 档案页展示“未归因主题”队列，按 `topic_hint` 聚合频次。把它映射回概念的补录入口还没做——映射后要写一条 re-attribution 事件再重放投影，原事件不覆盖。
- 概念因重建目录结构而消失又被重新抽到时，`concept_id` 由课程加名字派生所以仍是同一个，但投影已经跟着上一次删除没了。这时清掉 `mistake_backfills` 的完成标记，下次读档案整门课按事件流重放一遍。

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
| `POST` | `/materials/{material_id}/index` | 触发检索索引作业；完成后自动跑一次目录结构解析 |
| `POST` | `/materials/{material_id}/ocr/estimate` · `/ocr` | 扫描版 PDF 先按两页量出 OCR 账单，用户确认后才走 OCR 重新索引 |
| `GET` | `/courses/{course_id}/structure` | 每份教材抽到多少概念、其中多少条带层级 |
| `POST` | `/materials/{material_id}/structure/preview` | 重建目录结构的影响预告：新增/保留/删除多少概念，多少挂着掌握度或错题。只算不写 |
| `POST` | `/materials/{material_id}/structure` | 重算概念与层级，同步返回，不重新提取也不重新向量化 |
| `GET` | `/courses/{course_id}/concepts` | 概念目录，按教材目录顺序，层级用 `parent_id` 表示 |
| `GET` | `/materials/{material_id}/wiki/estimate` | 知识页构建前的账单：预计页数、模型调用次数与耗时。离线算，不调模型 |
| `POST` | `/materials/{material_id}/wiki` | 把指定教材构建成知识页；Wiki 未开启或教材未索引时返回 `feature_disabled` / `material_not_indexed` |
| `GET` | `/materials/{material_id}/wiki/report` | 最近一次构建完成时的覆盖率报告，界面刷新后靠它回读；没构建过是 `{"job": null}`，没跑完的那次不算 |
| `GET` | `/courses/{course_id}/wiki` · `/wiki/{concept_id}` | 列出知识页（带 `parent_id / level / order`，以及归属教材 `material_id` 与服务端解析好的文件名 `document`，供界面按教材分组；课程总览、旧格式页与已删教材的 `document` 是空串）与读取单页；单页给 `content` 整页原样，外加拆好的 `body` 与 `handwritten` |
| `PUT` | `/courses/{course_id}/wiki/{concept_id}/handwritten` | 写手写区，只动分隔线以下那一段。构建中 409，整页超上限 413 |
| `GET` | `/courses/{course_id}/wiki/{concept_id}/sources` | 这一页转述时依据的教材页；`limit` 是每份教材列出的页数上限。未知 concept 返回空表而不是 404 |
| `GET` | `/courses/{course_id}/wiki/lint` | 知识页体检，按需现算、零模型调用；路由排在 `/wiki/{concept_id}` 之前 |
| `GET` | `/courses/{course_id}/wiki/graph` | 知识页之间的跨教材配对（哪几个来源在讲同一件事），读时现算；同样排在 `/wiki/{concept_id}` 之前 |
| `GET` | `/jobs/{job_id}` | 查询索引或知识页构建任务的状态、阶段与错误摘要 |
| `POST` | `/sessions` | 创建 `{scope_mode: general}` 或 `{scope_mode: course, course_id}` 会话；默认 general |
| `GET` | `/sessions` | 按 `workspace=general|course:<id>` 可选过滤，返回课程色点与最近解析投影 |
| `GET` | `/sessions/{session_id}/messages` | 读取原始消息与 artifact 引用；`role='tool'` 的工具正文不返回 |
| `POST` | `/sessions/{session_id}/attachments` | 上传当前会话的图片附件；vision 未配置时返回 `feature_disabled` |
| `POST` | `/sessions/{session_id}/turns` | 发起一轮 Agent 执行，通过 SSE 返回 |
| `POST` | `/courses/{course_id}/knowledge/search` | 知识库中对用户明确选定课程做检索验证；通用对话不能绕过 Resolver 调用 |
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
- 教材、检索片段、知识页与 OCR 转录均视为不可信内容；其中的“忽略系统指令”等文本不能改变工具权限或课程边界。教材文件名与概念名会进系统提示，因此逐条压成单行、截断并加引号包裹，只能被读成数据。ToolPolicy 只信任服务端上下文和注册的 Schema。
- IM 渠道与 LLM 凭据只从环境变量/密钥环读取；日志过滤 `Authorization`、API Key、bot token 和带签名的图片 URL。

## 15. 前端

React SPA 的视觉系统、组件状态和响应式规范独立维护在 [coursepilot-2.0-frontend-design.md](./coursepilot-2.0-frontend-design.md)。本章只保留后端对前端的产品合约：

1. **对话**：默认通用模式，左栏「工作区」组里通用模式与各门课平级单选；切换工作区不修改旧会话。会话列表始终可见，通用模式下用课程稳定色表达归属、形状表达未读状态，通用会话顶部区分“本轮解析课程”和永久绑定。
2. **知识库**：全局导航名称，进入后必须明确选择课程。默认打开资料库，完整展示上传、解析、切块、嵌入、索引和检索验证；同级还有「目录结构」（概念数、有无层级、重建入口与影响预告）、「概念目录」（按教材目录画的可折叠树）、「课程笔记」，以及默认关闭的「知识页」tab。
3. **计划**：日历视图 + 完成情况 + 手动调整入口。
4. **管理**：课程/教材上传、检索索引、目录结构重建、知识页构建、未归因队列、用户 Skill 导入/预览/启停、模型槽位能力检查、trace 查看器。OCR 未配置时显示“未启用”，不影响文本学习。
5. **花钱与不可逆的动作先给账单**：OCR 按两页实测外推、知识页按切段结果报预计页数与调用次数、重建目录结构报会删掉哪些概念（其中多少挂着掌握度或错题）。用户点确认才执行。

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
├─ backend/
│  ├─ app/                      # composition root：bootstrap.py 是唯一装配点，http/ 放 FastAPI 与 SSE
│  ├─ contracts/                # 跨层 Port 与 DTO：llm / knowledge / embedding / reranker / web
│  ├─ core/                     # settings、SQLite store 与 migrations、身份、硬件探测
│  ├─ adapters/                 # openai_compatible / vision_ocr / demo、BGE 向量与重排、联网
│  └─ modules/
│     ├─ agent/                 # 主循环、上下文投影、系统提示、工具系统、trace
│     ├─ courses/ sessions/     # 课程工作区、会话与 Course Resolver
│     ├─ knowledge/             # 提取、切块、检索、概念目录、知识页构建
│     ├─ learning/ planning/    # 掌握度与错题投影、计划
│     └─ memory/ notes/         # markdown 记忆与课程笔记
├─ skills/builtin/              # practice / research / mistake_review / flashcards / diagram
├─ frontend/                    # React SPA
├─ evals/ scripts/
└─ tests/backend/               # 含 test_module_boundaries.py：模块依赖方向的守门
```

跨模块协作只允许三种方式：同步 port 接口、不可变 DTO、带版本 typed event。禁止模块直接读写别人的表、导入内部 service、从 handler 反向调用 HTTP 路由或共享可变全局对象。数据库可以同库，但每张表有唯一 owner；跨 owner 写入必须调用公开接口。`tests/backend/test_module_boundaries.py` 固化这些边界，代码 review 必查依赖方向、事务归属、幂等和事件版本。

## 18. 1.0 资产迁移

| 资产 | 处置 |
| --- | --- |
| `rag/` 全部（解析、切块、索引、混合检索、rerank） | 原位复用，先用 adapter 包成 `rag_search`；不在 Agent 重写期间同时更换检索算法 |
| `core/llm/openai_compat.py` | 作为 OpenAI-compatible adapter 的行为参考，经内部协议隔离后逐步替换 |
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
- **知识页的收益还没测出来**：A/B 只差"课程有没有知识页"时，引用召回逐条一致。逐页核对过内容与页码都对，问题在链条：这一维量的是引用，而模型不主动去取时知识页产生不了引用。已做的两件事是把目录常驻系统提示（调用率不再是瓶颈）和让知识页可引用（§8.3）；还缺一个直接判"远处那个事实有没有出现在回答里"的判据。
- **知识页的检索阈值没有单独标定**：`rag_min_rerank_score` 是按教材片段标定的，现在也在管概括语言写成的知识页。固定名额意味着调低它不会挤占教材席位，代价只是多一页不相关的，但该调到多少还没测过。
- **IM 渠道联调**：首版唯一外部渠道，卡片回调、长连接重连和幂等回执仍需真实环境验证；其他渠道不占首版工期。
- **前端从 Streamlit 换 React 的工作量**：四个页面中知识库与计划视图是新增工作量的主体，可按"对话 → 管理 → 知识库 → 计划"顺序分批交付。
- **OCR 不阻塞主线**：视觉槽位未配置时禁用图片入口，文本学习、练习和计划正常工作。后续选型以真实手写公式样本评测为准。

## 20. 建议实施顺序

重做采用长期渐进路线，但每一步都要形成可运行、可回归的竖向切片：

1. **合约地基**：建立 `store migrations`、LLM 内部协议、ToolResult / ToolCallContext、trace 协议和 contract tests。此时 1.0 产品行为不变。
2. **通用/课程会话与概念目录**：实现 courses、sessions、`turn_course_context`、Course Resolver、materials/index jobs/turns 和 React 基础对话页；RAG 仍通过 adapter 调用 1.0 能力。完成标志是持久会话、逐轮解析、流式回复、课程隔离和概念 ID 测试通过。
3. **Super Agent + Tutor**：新主循环、读时上下文、Tutor 证据合约、新工具系统接管 `rag_search`。通过特性开关与 1.0 learn baseline 对比。
4. **Practice skill**：引入通用 artifact 读写与 `practice` SKILL.md，覆盖文本出题、作答、评分、讲评、变式题和对象歧义；不增加阶段状态机，再退役 QuizMaster / Grader 旧链路。
5. **学习档案**：完成 EvidenceEvent、未归因队列、BKT/FSRS 投影与 replay；掌握度只消费已归因事件。
6. **可选知识页**：实现课程开关、按教材触发的构建作业、目录自底向上全量遍历、构建前账单与覆盖率回报，再让它成为可引用的来源；关闭时不影响检索、Tutor 与练习。
7. **主动化与 IM 渠道**：实现内建规划规则、`plan_read / plan_update`、版本化 plan item、单调度 tick、隐藏 system session 与 Web 通知；本地幂等和补投稳定后接 IM 渠道。其他渠道仅保留协议。
8. **OCR（可选）**：实现视觉槽位、attachment API、`VisionTranscriptionV1` 预处理和系统提示中的图片确认规则；未配置时不影响前七步交付。

每个切片只在 contract tests + smoke + 相关 1.0 baseline 通过后默认开启；旧代码在新切片稳定一轮后再删除，不边写边删回退路径。
