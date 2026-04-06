# CoursePilot Agent 项目全方面审阅与可执行改进报告

## 执行摘要

本次审阅覆盖你指定的仓库 **Eric-he-cn/course-pilot**，重点围绕多 Agent 编排方式、MCP 工具体系、记忆系统（SQLite）与上下文管理（ContextBudgeter + 工具回合压缩）进行代码级梳理，并输出“在不改变现有功能前提下”的重构与优化建议。主要依据来自仓库内的架构文档、核心源码与安全说明，并在仓库覆盖后补充引用 MCP 规范、RAG/RRF 原始论文与 OpenAI Structured Outputs 官方资料。

关键结论（按优先级/风险排序）：

1. **P0 级正确性风险：同步链路生成器陷阱**
   `core/orchestration/runner.py` 的同步模式函数（尤其 practice/exam 分支）存在 `yield`，会使函数变成生成器；而后端 `/chat`（非流式）期望得到结构化消息（`ChatMessage`），这类错配会导致 `/chat` 在对应模式下**不可用或行为异常**，且属于“容易被流式链路掩盖”的典型维护陷阱。

2. **上下文语义混装：把混合上下文当“教材参考”使用**
   ContextBudgeter 的 `final_text` 实际是“历史摘要/最近对话 + RAG 引用片段 + 记忆片段”的混合体，但 Tutor/Quiz/Exam 的提示词把 `{context}` 描述为“教材参考/教材内容摘要”，这会在引用、出题公平性与事实归因上造成偏差（尤其提示词要求 `\[来源N]` 时）。

3. **记忆检索重复调用 + 工具去重机制交织**
   Runner 预取记忆、QuizMaster 生成题目/试卷再次 `memory_search`，导致同一请求内重复检索与 token 冗余，并与 `LLMClient` 的 request-scope cache/dedup 交错，形成“看似有缓存但实际仍重复”的性能黑盒。

4. **工具策略与产品语义不一致（考试联网）**
   前端帮助文案强调“考试模式禁用联网搜索”，但 `ToolPolicy` 三模式默认全放行，测试也断言 exam 可 websearch；Router Prompt 还让模型输出 `allowed_tools`，但代码会覆盖此字段。结果是：**UI/Prompt/Policy 三者事实不一致**，不利于长期维护与安全约束。

5. **RAG 可能过度压缩（二次压缩）**
   Retriever 句级压缩 + ContextBudgeter 再压缩 + 工具回合再抽取摘要，会削弱证据完整性。RAG 的价值之一是“可追溯证据/可更新性”，需要避免压缩链路叠加导致引用证据不足。

---

## 为完成高质量审阅必须学习的关键信息点

结合你的审阅目标与仓库现状，我认为完成“全方面且可执行”的评审报告，至少必须搞清以下 5 个信息点（已在本报告中逐一覆盖并落到建议）：

1. **Runner 端的多 Agent 编排数据流**：Plan →（RAG/Memory）→ ContextBudget → Tutor/Quiz/Grader 的调用次序、依赖与失败兜底。
2. **MCP 工具调用协议与实现细节**：stdio JSON-RPC 帧格式、initialize/tools/list/tools/call 支持范围、工具 schema 与真实执行路径是否一致。
3. **上下文管理的“来源分层”与预算策略**：历史摘要、RAG 引用、记忆片段在何处合并、如何压缩、如何避免误标为“教材证据”。
4. **记忆系统的数据结构与检索方式**：episodes/user_profiles 的字段、写入触发点、检索质量与性能瓶颈（LIKE 搜索），以及与工具链的交互方式。
5. **各 Agent 的 Prompt 约束与可验证输出机制**：是否存在“格式/输出约束不足”导致的不可控输出；是否能用 Structured Outputs/JSON mode 提升确定性与可维护性。

---

## 架构与数据流梳理

整体是“Streamlit 前端 + FastAPI 后端 + 编排 Runner + 多 Agent + RAG + MCP 工具 + SQLite 记忆”的组合架构。前端主要通过 `/chat/stream` 走 SSE 流式输出（并额外约定 `__citations__`、`__tool_calls__`、`__context_budget__`、`__status__` 等 meta 事件），后端负责将 Runner 输出包装为 SSE。

### 端到端调用链

```mermaid
flowchart TD
  UI[Streamlit 前端] -->|SSE /chat/stream| API[FastAPI 后端 api.py]
  UI -->|POST /chat| API

  API --> RUN[OrchestrationRunner]
  RUN --> ROUTER[RouterAgent plan/replan]
  RUN --> RETR[Retriever: dense/bm25/hybrid]
  RETR --> FAISS[(FAISS index + chunks.pkl)]
  RETR --> BM25[BM25Index]
  RETR --> EMB[EmbeddingModel]

  RUN --> BUD[ContextBudgeter]
  BUD --> CTX[final_text: history + rag + memory]

  RUN -->|learn| TUTOR[TutorAgent]
  RUN -->|practice/exam 生成题| QUIZ[QuizMasterAgent]
  RUN -->|practice/exam 评分| GRADER[GraderAgent]

  TUTOR --> LLM[LLMClient(chat or tools)]
  QUIZ --> LLM
  GRADER --> LLM

  LLM -->|tool calls| MCP[MCPTools.call_tool]
  MCP -->|stdio client| MCPCLI[_StdioMCPClient]
  MCPCLI -->|Content-Length frames| MCPSRV[mcp_tools.server_stdio]

  MCPSRV --> TOOLIMPL[calculator/websearch/memory_search/mindmap/filewriter/get_datetime]
  TOOLIMPL --> MEM[MemoryManager]
  MEM --> SQLITE[(SQLite episodes & user_profiles)]
```

上述链路由仓库架构文档与对应实现共同支持：MCP server 仅承载工具入口，工具业务逻辑在同一个代码库内执行，并通过 stderr 打印防止污染 stdout 协议帧。

### 多 Agent 编排方式与接口依赖

当前是“中心编排器（Runner）+ 专用 Agent（Router/Tutor/QuizMaster/Grader）”的形态：

* **RouterAgent**：输出 `Plan`，并提供 `replan()`。其 `_normalize_plan()` 会强行按 `ToolPolicy` 覆盖 `allowed_tools` 与 `task_type`。
* **TutorAgent**：构造 system/user messages，并在启用工具时通过 `LLMClient.chat_with_tools` 调起工具链；system prompt 中包含“Plan/Act/Synthesize 三阶段”的自然语言规约。
* **QuizMasterAgent**：强制 JSON 输出（题目与试卷），包含解析失败时的 LLM 修复器；在必要时调用 `get_datetime` 与 `websearch`（通过 MCPTools）。
* **GraderAgent**：暴露 calculator 工具 schema，并写入错题记忆与更新弱点；流式评分也注入“内部评分计划”到 system prompt。

这套切分整体合理，但目前 Runner 的“上下文拼装/记忆预取/模式状态机”承载过多隐含规则，容易形成“看起来能跑，但难稳定演进”的结构风险。

---

## 模块清单表

> 说明：表格聚焦仓库内“核心路径”与“与改造相关的关键文件”。所有条目均来自你指定仓库。

| 文件/模块                           | 关键类/函数                                     | 主要职责                                             | 主要问题                                                                                                              | 建议（不改功能前提）                                                                                                   |
| ------------------------------- | ------------------------------------------ | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `core/orchestration/runner.py`  | `run` / `run_stream`、各 mode handler        | 编排核心：Router/RAG/ContextBudget/Agent 分发、meta 事件生成 | 1) 同步 practice/exam 分支存在 `yield`→生成器陷阱（破坏 `/chat`）；2) 混合上下文被当作教材；3) memory 预取与 QuizMaster 再取重复；4) 索引加载/缓存失效策略不清晰。 | P0 修复同步链路；P1 拆分上下文为 history/rag/memory 三段；P1 统一 memory 预取策略；P1 增加 index 版本/mtime 校验与 store cache invalidate。 |
| `core/agents/router.py`         | `plan`/`replan`                            | 将自然语言映射为 `Plan`                                  | Prompt 让模型输出 `allowed_tools`，但代码覆盖该字段；Plan 没有显式 `need_memory`/`need_citations`，导致编排判断散落在多处。                       | 让 Prompt/Plan schema 与代码一致（删字段或真正使用）；扩展 Plan（保持默认，向后兼容）。                                                     |
| `core/orchestration/prompts.py` | 全量 prompts                                 | 维护 Router/Tutor/Quiz/Exam/Grader prompts         | 1) 多处“教材/上下文”语义不一致；2) JSON 输出规约对齐不足（尤其 Router）；3) exam 的工具约束与 UI/Policy 不一致。                                      | 统一 prompt 模板的“上下文分区”格式；Router/Quiz/Exam 引入更严格 JSON schema/示例；exam 明确禁止 websearch（若产品要求）。                     |
| `core/agents/tutor.py`          | `_build_messages`/`teach`                  | learn 模式回答与工具调用                                  | 1) system prompt 规则编号不连续；2) 提到“数据库”但实际是 RAG/文件；3) `TutorResult` 定义了 citations/log，却未被链路填充（结构未闭环）。                 | 精炼 system prompt；将“数据库”替换为“课程知识库/教材索引”；要么补齐 TutorResult 的结构化输出，要么删掉未用字段。                                     |
| `core/llm/openai_compat.py`     | `chat_with_tools`/`chat_stream_with_tools` | OpenAI 兼容调用 + Act/Synthesize + 工具门控/去重/压缩        | 与 ContextBudgeter 重复压缩；工具失败 fallback 逻辑复杂但缺少统一测试覆盖；依赖版本差异风险（pyproject vs requirements）。                           | 压缩责任收敛；为工具回合写覆盖测试；统一依赖来源（仅保留 Poetry 或 requirements + lock）。                                                  |
| `mcp_tools/client.py`           | `_StdioMCPClient`/`MCPTools.call_tool`     | 工具 schema + stdio MCP client + 本地工具实现            | MCP client 读取采用线程 join，性能/复杂度可接受但可优化；websearch 依赖 SerpAPI Key；mindmap 内部又会拉 RAG。                                  | 增加工具超时/取消（MCP utilities）；mindmap 的 RAG 逻辑与 Runner RAG 合并；测试中对 websearch 做“无 key 预期失败”。                       |
| `mcp_tools/server_stdio.py`     | `tools/list`/`tools/call`                  | stdio MCP server                                 | 功能最小子集，未实现 progress/cancel/ping；够用但不完整。                                                                           | 逐步补齐 utilities（至少 progress）；或明确“仅工具调用，无进度协商”的限制。                                                             |
| `memory/manager.py`             | `record_event`/`get_profile_context`       | 记忆接口与画像更新                                        | 全局单例 + SQLite 连接 per call；画像字段较丰富但 episode 检索质量受限；多处重复写入统计。                                                       | episode 检索引入 SQLite FTS5；写入点统一为 `record_event`（减少分散）；为 user_id 多租户预留接口。                                      |
| `memory/store.py`               | `search_episodes`                          | SQLite 存储与检索                                     | 检索为 LIKE OR，规模增大会慢；缺少全文索引；metadata 过滤在 Python 侧做二次过滤。                                                             | 增加 FTS5 虚表 + 触发器同步；把 mode/agent/phase 设计为冗余列以便 SQL 过滤。                                                       |
| `rag/retrieve.py`               | hybrid 检索 + RRF                            | dense/BM25/hybrid；句级压缩；引用格式化                     | 二次压缩链路风险；BM25 中文分词太粗；RRF 正确但缺少 reranker。                                                                          | 保留一次压缩即可；BM25 引入更好分词或 FTS；可选引入 reranker。                                                                     |
| `docs/guides/security.md`              | 安全说明                                       | 已实现控制/已知风险/上线加固                                  | 明确提示缺少认证/限流/CORS 收敛等生产风险。                                                                                         | 把该文档建议转化为真实的 middleware 与配置（P1/P2）。                                                                          |

---

## Prompt 审查与逐项优化清单

> 说明：本节按“原始 Prompt 摘要 → 问题 → 优化后示例/模板”给出可直接替换的 Prompt 方案。所有原始内容来自 `core/orchestration/prompts.py`。
> 目标是：减少歧义、补齐输出约束、让 prompt 与代码事实一致，并为后续 Structured Outputs 升级铺路。

### Router Prompt

**原始**：要求输出 JSON：need_rag/allowed_tools/task_type/style/output_format；并在模式说明中写“exam 允许所有工具”。

**问题**：

1. RouterAgent 会覆盖 `allowed_tools` 与 `task_type`，Prompt 要模型输出这些字段属于“无效约束”，反而增加误导。
2. 缺少对 JSON 严格性的约束（例如不得输出 markdown），`RouterAgent._extract_json_payload` 仅做了简单 code fence 兼容。
3. 缺少“是否需要记忆检索/是否需要引用”的显式字段，导致 Runner/Agent 自行判断，逻辑分散。

**建议优化后的 Prompt（示例）**（兼容当前 Plan，新增字段可先忽略/默认）：

```text
你是课程学习助手的【任务规划器】。你的输出将被程序解析，因此必须严格只输出一个合法 JSON 对象，不要输出任何解释、前后缀、markdown 代码块。

输入：
- mode: {mode}
- course_name: {course_name}
- user_message: {user_message}
{weak_points_ctx}

输出 JSON 字段（必须齐全）：
{
  "need_rag": true|false,
  "need_memory": true|false,
  "style": "step_by_step"|"hint_first"|"direct",
  "output_format": "answer"|"quiz"|"exam"|"report",
  "notes": "<=80字，给编排器的短备注，可为空字符串"
}

判定规则：
- need_rag: 用户问题需要教材证据/章节引用/概念定义/公式来源时为 true；纯闲聊/纯操作指令且无需教材为 false。
- need_memory: 用户提到“上次/之前/历史/错题/薄弱点/复习”或需要个性化复盘时为 true，否则 false。
- style: 若与薄弱点相关或明显不理解则 step_by_step；若用户要提示而非答案则 hint_first；若用户要直接结论则 direct。
- output_format: 练习出题为 quiz；考试流程为 exam；其余为 answer 或 report。
```

这能让 Router 输出与 Runner/ToolPolicy 的事实更一致，并将“记忆检索”显式化，从而减少重复记忆调用。

---

### Tutor Prompt

**原始**：把 `{context}` 描述为“教材参考资料（每段已标注来源编号）”，要求内联 `[来源N]`。

**问题**：

1. 真实 `{context}` 很可能被 Runner 传入混合上下文（历史+记忆+教材），会造成“把历史/记忆误当教材来源”的风险。
2. 引用规则只写了“不要单独列出引用列表”，但没有强调“引用只能来自教材块”，也没有强调“若无相关来源，必须显式声明”。

**建议优化后的 Prompt（示例）**：将上下文分区，并对引用来源做强约束。

```text
你是一位大学课程学习导师。请严格基于【教材证据】回答，并在使用教材信息时以 [来源N] 内联标注。

【教材证据】（仅此部分可作为引用来源）：
{rag_context}

【对话摘要】（用于保持连续性，不得当作教材引用来源）：
{history_context}

【学习档案/记忆摘要】（用于个性化建议，不得当作教材引用来源）：
{memory_context}

用户问题：
{question}

输出结构：
1) 核心答案（必须：若使用教材信息则带 [来源N]）
2) 详细解释（定义/推导/例子，引用仍限于教材证据）
3) 关键要点与易错点
4) 1-2 句话总结

强约束：
- 若【教材证据】为空或不包含相关信息：必须明确说明“教材未覆盖/检索为空”，并给出下一步（如建议上传章节/换问法）。
- 不得伪造 [来源N]；不得把对话摘要/记忆摘要当作引用来源。
```

该改造的前提是 Runner 需要把 context 拆成三段（见后文重构任务）。

---

### Quiz/Exam Prompt（QUIZZER_PROMPT、EXAM_GENERATOR_PROMPT、EXAM_PROMPT）

**原始**：Quiz 要求 JSON 输出题目/答案/rubric；Exam prompt 是三阶段自然语言状态机；QuizMaster 实际还有“内部计划（Plan）”与 JSON 修复器。

**问题**：

1. 仍依赖模型遵守 JSON 输出，而不是协议层保证；QuizMaster 已写修复器，意味着格式不稳定是已知问题。
2. exam “禁用联网搜索”在提示词里有，但策略层不强制；同时 QuizMaster 自己也会在必要时调用 websearch/get_datetime。

**建议**：

* 若你继续使用 OpenAI Chat Completions 的 function calling/Structured Outputs，应考虑将 Quiz/Exam 输出改为 **Structured Outputs**（严格 JSON schema），以减少修复器与解析逻辑。OpenAI 官方指出 Structured Outputs 可在 strict 模式下保证 schema adherence。
* 在 “不改功能” 的阶段，可以先做 **Prompt 与解析对齐**：强调“仅输出 JSON，无 markdown”，并在 JSON 字段中加入 `additionalProperties: false` 的等价自然语言约束（或在 Structured Outputs 中直接表达）。

---

### Grader Prompt（GRADER_PROMPT / GRADER_PRACTICE_PROMPT / GRADER_EXAM_PROMPT）

**原始**：要求必须调用 calculator 汇总总分，禁止心算；输出 JSON。

**问题**：

1. 依赖模型自觉；代码侧对“score 是否来自 calculator”没有强一致性校验；
2. PracticeGradeSignal 仍通过正则从自由文本抽分数（属于脆弱路径，如果未来格式变化，记忆写入会误判）。

**建议优化**：评分输出改为强 schema（Structured Outputs），并在代码侧把 calculator tool 的返回值作为最终 score 的唯一来源。

---

## 上下文管理审阅与逐模式逐 Agent 优化建议

### 现状：两层上下文压缩 + “语义混装”

* ContextBudgeter 会：

  * 保留最近 N 轮 raw（`recent_raw_turns`），更老对话做摘要卡片（可启用 LLM 压缩）
  * 压缩 RAG 文本（按【来源块】与关键词挑选句子）
  * memory_text 裁剪
  * 组合为 `final_text`，并记录 `context_budget` 指标。

* LLMClient 在工具回合（Act 多轮）中又会：

  * 对 messages 进行 compaction，保留 system + compact user + 最近工具段
  * 从 user content 中提取“教材/记忆”片段做摘要（_build_compact_user_content）。

### 主要问题

1. **最小化目标不清晰**：
   ContextBudgeter 的 `final_text` 已经是“预算内最小化版本”，工具回合又再缩一次，可能导致证据不足。

2. **分区语义缺失**：
   Tutor prompt 把 `{context}` 当“教材参考”，但它包含 history 与 memory，使引用规范被污染。

3. **逐 Agent/逐模式差异未显式表达**：
   practice/exam 多阶段对话对历史依赖更重，但当前 budgeter/runner 主要做统一策略，缺少模式级参数调度。

### 建议的上下文“分层最小化”方案

核心原则：**把上下文拆成 3 个显式分区**，并明确“哪些分区可引用、哪些仅供连续性/个性化”。所有 Agent 的 prompt 统一使用这三个分区。

* `history_context`：用于连续性（摘要卡片 + 最近对话）
* `rag_context`：仅教材证据（带 [来源N]）
* `memory_context`：个性化（弱点、错题摘要）

并给每个 Agent 明确“需要哪些分区”。下表给出“可删除/可合并项”与替代方案。

### 上下文优化建议表

| 模式       | Agent                         | 当前上下文（推断）                                       | 可删除/合并点                                                    | 替代方案（可执行）                                                                                          |
| -------- | ----------------------------- | ----------------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| learn    | TutorAgent                    | `final_text`(history+rag+memory) 作为 `{context}` | 1) 将 history/memory 从“教材参考”剥离；2) 若启用工具回合压缩，避免二次压缩 rag。     | Runner 调用 budgeter 时返回 three contexts；Tutor prompt 采用分区模板；工具回合 compaction 仅压缩 history，不再压缩 rag。    |
| practice | QuizMasterAgent               | Runner ctx + 自身 memory_search + external ctx    | memory_search 重复；external ctx 与考试/题目语义可能冲突；题目不应吸收历史 raw 细节 | Runner 提供 `memory_context` 供 QuizMaster；QuizMaster 增加参数 `prefetched_memory_ctx`，优先用它；history 只给摘要。 |
| practice | GraderAgent                   | 题面 + 学生答案 + history_ctx（错题摘要）                   | history_ctx 可能过长且影响评分确定性                                   | history_ctx 只保留“错题标签/弱点”列表，不给长文本；评分只基于题面标准答案与 rubric。                                              |
| exam     | QuizMasterAgent / Tutor(Exam) | 三阶段对话 + context 目录摘要                            | “禁用 websearch”应在策略层硬禁；history 要保留状态但必须最小                   | `ToolPolicy` exam 禁用 websearch；history_context 提供结构化 state（阶段、已收集配置）。                              |
| all      | 工具回合（LLMClient）               | 从 user prompt 再抽取 rag/memory 摘要                 | 与 ContextBudgeter 重复压缩                                     | 仅当 Runner 没做 budget（或工具回合>1且超预算）才启用；默认关闭 rag/memory 再摘要。                                           |

---

## 重构任务清单（优先级 / 估时 / 风险）

> 估时按低/中/高：低（<1 天）、中（1-3 天）、高（>3 天）。
> 风险点主要指：行为兼容性、接口变更面、回归测试需求。

| 优先级 | 任务                                       | 目标/收益                                | 主要改动点                                                                                       | 估时 | 风险点与控制                                                        |
| --- | ---------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------- | -- | ------------------------------------------------------------- |
| P0  | 修复同步链路生成器陷阱                              | `/chat` 在 practice/exam 模式可用；消除死代码陷阱 | `runner.py`：把同步路径改为返回 `ChatMessage`，流式用 `run_stream`；禁止同步函数含 `yield`。                       | 低  | 风险：影响 API 返回结构；控制：加 test 覆盖 `/chat` 三模式。                      |
| P0  | 统一“考试禁用联网”语义（若产品要求）                      | UI/Policy/Prompt 三者一致；减少安全风险         | `ToolPolicy.MODE_POLICIES`：exam 移除 websearch；前端帮助文案与 tests 对齐。                              | 低  | 风险：行为变化（若之前允许考试联网）；控制：将其设为环境开关（EXAM_ALLOW_WEBSEARCH=0/1）。     |
| P1  | 上下文分区：history/rag/memory 显式化             | 引用规范稳定；减少 Prompt 语义错位                | `ContextBudgeter.build_context` 返回三段；Runner 传入 Tutor/Quiz/Grader；重写 Tutor prompt。           | 中  | 风险：Prompt 输入格式变化；控制：增加兼容层（旧 `{context}`=拼接三段）。                |
| P1  | 记忆检索去重：Runner 统一预取并传递                    | 降低重复工具调用与延迟                          | Runner 增加 `prefetched_memory`；QuizMaster 接受注入并跳过二次 memory_search。                           | 中  | 风险：参数改动影响调用；控制：保持默认行为（无注入则自取）。                                |
| P1  | RAG 压缩责任收敛                               | 减少证据丢失与“二次压缩”                        | 选择“Retriever 句级压缩”或“Budgeter 压缩”二选一；建议保留 Retriever 的 chunk 级压缩，budgeter 只做 token 裁剪，不再二次选句。 | 中  | 风险：答案引用可能变化；控制：用 benchmarks 评估 hit@k/precision（仓库已有 perf 脚本）。 |
| P1  | 依赖与环境统一（pyproject vs requirements）       | 构建可复现，减少“装出来不一样”                     | 二选一：Poetry 为主（生成 lock）或 requirements + pip-tools；当前两份存在差异：openai/faiss-cpu/numpy/torch。     | 中  | 风险：升级依赖影响行为；控制：先锁版本，再逐步升级。                                    |
| P2  | 记忆检索升级为 SQLite FTS5                      | 随数据增长保持性能；提高召回质量                     | 新增 FTS5 虚表 episodes_fts；写入同步；search_episodes 改为 MATCH + bm25。                               | 高  | 风险：迁移脚本/数据一致性；控制：双写/回退（保留 LIKE 路径）。                           |
| P2  | Quiz/Exam/Grader 输出改用 Structured Outputs | 移除 JSON 修复器与脆弱正则；输出可验证               | 引入 OpenAI Structured Outputs（strict schema）；失败回退到旧解析路径。                                     | 高  | 风险：模型/SDK 兼容性；控制：后端加 feature flag，分阶段上线。                      |
| P2  | MCP utilities（progress/cancel）支持         | 长任务可观测 + 可取消                         | 扩展 MCP server 支持 progress 通知或 logging；前端展示进度。                                               | 高  | 风险：协议扩展复杂；控制：先实现 progress（只读），再做 cancel。                      |

---

## 可执行补丁建议（示例级）

> 由于 GitHub 连接器不支持一次性拉取全仓库 zip/目录树（只能按文件 fetch），我在此提供“最关键的 P0 修复”和“上下文分区 P1 改造”的示例补丁思路。你可以据此在本仓库内直接落地；若需要我继续生成更完整的 diff，我可以基于更多文件逐个 fetch 后补齐。

### P0：修复 runner 同步路径 generator 陷阱（概念性补丁）

**症状**：同步模式函数中出现 `yield`，导致函数变为生成器，与 `/chat` 期望返回类型不一致。

**建议**：同步接口只返回 `ChatResponse/ChatMessage`；流式只用 `run_stream`。

伪 diff（示意）：

```diff
- def run_practice_mode(...)-> ChatMessage:
-     ...
-     yield {"__citations__": ...}
-     return ChatMessage(...)
+ def run_practice_mode(...)-> ChatMessage:
+     ...
+     # 同步模式不产生 meta 事件，直接把 citations 放入 message.citations
+     return ChatMessage(
+         role="assistant",
+         content=answer_text,
+         citations=citations,
+         tool_calls=tool_calls,
+     )
```

同时在 `backend/api.py` 的 `/chat` 端点：确保只调用 `runner.run(...)` 并返回 pydantic model；`/chat/stream` 端点只 yield 字符串与 meta 事件。

### P1：上下文分区（history/rag/memory）落地方式

基于现有 `ContextBudgeter.build_context` 的返回结构，其实已经分别计算了 `history_text`、`rag_text`、`memory_text`，只是最终又合并为 `final_text`。

建议：Runner 不再把 `final_text` 当作“教材参考”，而是传递三段，并让 Prompt 明确“引用只能来自 rag_context”。

伪代码示意：

```python
ctx = budgeter.build_context(...)
history_ctx = ctx["history_text"]
rag_ctx = ctx["rag_text"]          # 已带 [来源N]
memory_ctx = ctx["memory_text"]

tutor_prompt = TUTOR_PROMPT_V2.format(
  rag_context=rag_ctx,
  history_context=history_ctx,
  memory_context=memory_ctx,
  question=user_message,
)
```

---

## 未指定假设与对建议的影响

你明确指出以下信息未指定：运行环境、目标部署平台、测试用例与 CI 状态。结合仓库现状，我补充列出具体影响：

1. **运行环境（Python 版本、OS、CPU/GPU）未固定**

* `pyproject.toml` 约束 python `^3.9`，但 `requirements.txt` 中 torch 版本与 CUDA 注释较激进（>=2.7.0），且包含 `pywin32`（Windows only）与 FAISS Windows unicode path workaround。不同 OS/硬件差异会影响：索引构建耗时、embedding 设备选择、PPT 解析能力。
  **影响**：重构建议中涉及性能/并发（如 MCP client、FAISS index 类型、后台队列）应在目标环境做基准验证（仓库已有 perf 脚本可用）。

2. **目标部署平台未指定（本地/校园网/公网、单机/容器/K8s）**

* 安全文档明确指出缺少认证、CORS 过宽、限流缺失，不适合直接公网暴露。
  **影响**：若你计划公网部署，则 P1/P2 的“鉴权/限流/隔离/审计”应提前，甚至优于部分功能性重构。

3. **测试用例与 CI 未指定且看不到 GitHub Actions**

* 仓库存在 `tests/test_basic.py`（非 pytest），但未见 `.github/workflows`。
  **影响**：P0/P1 改动（尤其 Runner）容易引入回归，需要尽快把关键链路（/chat、/chat/stream、build-index、tool calling）纳入自动化测试并在 CI 跑起来，否则后续 refactor 风险将显著上升。

4. **依赖管理策略未统一（Poetry vs requirements）**

* `pyproject.toml` 与 `requirements.txt` 存在版本差异（例如 openai、faiss-cpu、numpy 等）。
  **影响**：任何涉及 SDK 行为（如 tool calling、Structured Outputs）的改造，都需要先统一依赖来源，避免“某台机器能跑、另一台不行”。

---

## 外部资料补充引用（用于支撑技术路线）

为支撑“RAG/Hybrid 检索/结构化输出/MCP”相关建议，我补充引用如下权威来源（优先原始论文/官方文档）：

* **RAG 原始论文（NeurIPS 2020）**：强调“显式非参数化记忆+可追溯证据”的价值，为本项目“引用证据稳定性”与“避免过度压缩”提供理论支撑。
* **RRF 原始论文（SIGIR 2009）**：项目 hybrid 检索使用 RRF 融合，与经典方法一致；后续可用于解释参数 `k=60` 的意义与调参路径。
* **MCP 规范（2024-11-05 revision）**：用于校验当前 stdio initialize/tools/list/tools/call 流程，并指导 progress/cancel 等 utilities 的演进。
* **OpenAI Structured Outputs 官方指南**：用于替换“强 Prompt + JSON 修复器”的不稳定路径，提升 Quiz/Exam/Grader 输出的可验证性。
* **SQLite FTS5 官方文档**：用于在不引入外部服务的前提下，提高记忆检索质量与性能。

