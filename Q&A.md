#  Q&A

## 使用说明

## 1. 整体项目（架构/多Agent/前后端/上下文）

### 1.1 这个项目的主架构是什么？
- 标准回答：主架构是“前端 Streamlit + 后端 FastAPI + Core 编排器 + RAG + MCP 工具 + 记忆系统”。核心编排由 `OrchestrationRunner` 统一路由 learn/practice/exam 三个模式。
- 详细补充：可以补一句“Runner 是唯一编排入口”，强调任何模式切换最终都在编排层收敛，便于排障。
- 代码锚点：`core/orchestration/runner.py:30`，`backend/api.py:341`，`backend/api.py:366`

### 1.2 Agent 之间如何“通信”？
- 标准回答：不是 Agent-to-Agent 对话网络，而是 Runner 中央编排。Runner 调 Router 产计划，再调用 Tutor/Grader/QuizMaster；工具调用统一经 LLM function-calling -> MCP。
- 详细补充：回答时点明“不是分布式消息总线”，而是函数级调用链，这样能解释为什么调试主要看 Runner 日志。
- 代码锚点：`core/orchestration/runner.py:702`，`core/orchestration/runner.py:721`，`core/llm/openai_compat.py:133`

### 1.3 为什么用多个 Agent，而不是一个 Agent+多提示词？
- 标准回答：当前差异不只提示词，还包括执行路径与约束：Router 产结构化 Plan，Tutor 支持工具流式教学，Grader 强制 calculator 评分链路，QuizMaster 专职出题/出卷。职责分离能降低提示词耦合和回归风险。
- 详细补充：补充“职责分离后可分别做回归测试”，避免一个大 prompt 改动影响全部能力。
- 代码锚点：`core/agents/router.py`，`core/agents/tutor.py`，`core/agents/grader.py`，`core/agents/quizmaster.py`

### 1.4 多轮对话是怎么实现的？
- 标准回答：前端会先裁剪最近历史并发送后端；后端在 Runner 先做 recent turns 截取，再由 `ContextBudgeter` 按 `history -> rag -> memory -> hard_truncate` 统一做 token 预算。Tutor 默认不重复注入原始历史（可配置开启）。
- 详细补充：现在是“前端裁剪 + Runner 预算器 + Agent 可选原文注入”三层控制，不再是单纯 `history_limit` 拼接。
- 参数速记：`CB_HISTORY_RECENT_TURNS=6`、`CB_RECENT_RAW_TURNS=3`、`CB_INCLUDE_RAW_HISTORY_IN_MESSAGES=0`（默认）。
- 代码锚点：`frontend/streamlit_app.py`，`backend/api.py`，`core/orchestration/runner.py`，`core/orchestration/context_budgeter.py`，`core/agents/tutor.py`

### 1.5 上下文过长如何处理？
- 标准回答：当前已启用 token 级 `ContextBudgeter`。会先压历史（含滚动摘要与可选 LLM 压缩）、再压 RAG（句级压缩）、再压 memory，最后做 hard truncate，避免超预算。
- 详细补充：相比早期“只按轮次截断”，现在是分段预算 + 条件触发压缩，长对话与工具链路更稳。
- 参数速记：`CTX_TOTAL_TOKENS=8192`、`CTX_SAFETY_MARGIN=256`、`CB_HISTORY_SUMMARY_MAX_TOKENS=700`、`CB_LLM_COMPRESS_TRIGGER_TOKENS=600`、`CB_LLM_COMPRESS_TARGET_TOKENS=260`。
- 代码锚点：`core/orchestration/context_budgeter.py`，`core/orchestration/runner.py`

### 1.6 后端存在的核心价值是什么？
- 标准回答：后端承担课程工作区管理、上传解析、索引构建、流式 SSE 输出、编排入口与安全边界（路径/文件管理）。前端直连 core 会丢失这些统一服务能力。
- 详细补充：补充“后端是文件与索引的可信边界”，把路径校验、异常语义和流式输出统一托管。
- 代码锚点：`backend/api.py:5`，`backend/api.py:341`，`backend/api.py:366`

### 1.7 三种模式的工具权限是如何控制的？
- 标准回答：当前策略是三模式都允许同一套工具（`ALL_TOOLS`），差异主要在提示词和流程阶段控制，不在白名单差异。
- 详细补充：可以说明当前工具白名单一致是 MVP 取舍，未来若做风控可按模式细分可用工具。
- 代码锚点：`core/orchestration/policies.py:12`，`core/orchestration/policies.py:27`

### 1.8 三种模式的分流逻辑是什么？
- 标准回答：先由 Router 产 Plan，再由 Runner 根据 `mode` 分发到 `run_learn_mode* / run_practice_mode* / run_exam_mode*`。这是单入口、多流程分支。
- 详细补充：建议加一句“先计划后执行”能让 learn/practice/exam 流程差异有明确代码落点。
- 代码锚点：`core/orchestration/runner.py:690`，`core/orchestration/runner.py:702`，`core/orchestration/runner.py:780`

### 1.9 用户如何知道系统“正在工作”而不是卡死？
- 标准回答：流式工具链路会发 `__status__` 事件（如检索/工具执行状态），前端识别后用进度文案显示，最终继续输出正文。
- 详细补充：可强调状态事件不写入最终答案，只用于用户反馈，避免污染正文内容。
- 代码锚点：`core/llm/openai_compat.py:198`，`frontend/streamlit_app.py:717`

### 1.10 引用显示为何不串历史？
- 标准回答：后端先发 `__citations__` 事件，前端按轮次缓存到 `_pending_citations`，流结束后 pop 到当前 assistant 消息并入历史；不会把旧轮引用混入新轮。
- 详细补充：关键点是“本轮引用本轮绑定”，通过 pending 缓存避免多轮引用串台。
- 代码锚点：`core/orchestration/runner.py:674`，`frontend/streamlit_app.py:699`，`frontend/streamlit_app.py:732`

### 1.11 整体项目追加拷打（15）

### 1.12 为什么要把所有模式收敛到一个 Runner，而不是每个模式单独一套入口？
- 标准回答：统一入口能把“路由、检索、工具调用、记忆写回、流式事件”集中治理，避免三套流程长期漂移。这样上线后排障只需要先看 Runner 主链路，而不是在多个入口函数里来回跳。
- 详细补充：面试时可以强调这属于“中心编排”设计，牺牲了一些局部自由度，换来一致性和可维护性。
- 代码锚点：`core/orchestration/runner.py:30`，`core/orchestration/runner.py:690`，`core/orchestration/runner.py:780`

### 1.13 为什么同时保留 `/chat` 和 `/chat/stream` 两个接口？
- 标准回答：`/chat` 适合同步场景（测试、脚本调用、结构化返回），`/chat/stream` 适合交互场景（降低等待焦虑、可展示状态进度）。两者共用同一编排内核，只是输出协议不同。
- 详细补充：这是“同一业务逻辑，多种交付形态”的接口设计，能兼顾稳定调用与产品体验。
- 代码锚点：`backend/api.py:334`，`backend/api.py:356`，`frontend/streamlit_app.py:378`

### 1.14 Router 规划结果解析失败时，系统怎么保证不中断？
- 标准回答：Router 对模型输出做 JSON 解析，失败时直接回退默认 Plan（need_rag=true、按模式取 allowed_tools），让主链路继续执行，不把解析失败扩大为整次请求失败。
- 详细补充：这体现了“模型不可信、编排要兜底”的工程原则，避免把可恢复错误变成致命错误。
- 代码锚点：`core/agents/router.py:41`，`core/agents/router.py:53`，`core/agents/router.py:92`

### 1.15 模式差异是靠提示词硬控，还是靠流程分支硬控？
- 标准回答：两者都有，但主导是流程分支硬控。Runner 在入口按 mode 进入 learn/practice/exam 不同函数，提示词是该分支内部的一层策略，不是唯一控制手段。
- 详细补充：面试回答时可以强调“提示词负责行为风格，流程分支负责业务语义”。
- 代码锚点：`core/orchestration/runner.py:705`，`core/orchestration/runner.py:709`，`core/orchestration/runner.py:793`

### 1.16 工具调用环路如何防止模型无限调用工具？
- 标准回答：在 `openai_compat` 里走显式 `Plan/Act/Synthesize`。Act 有轮次上限（默认 `ACT_MAX_ROUNDS=4`），超过上限会直接进入最终 Synthesize，避免工具死循环拖垮时延和成本。
- 详细补充：这是典型“保护阈值”机制，防止极端 prompt 或模型异常导致不可控循环。
- 参数速记：`ACT_MAX_ROUNDS=4`、`ACT_MAX_TOKENS=160`、`ALWAYS_FINAL_STREAM=1`（默认）。
- 代码锚点：`core/llm/openai_compat.py`

### 1.17 当工具执行失败时，系统是直接报错还是有降级策略？
- 标准回答：工具层失败会记录日志并返回结构化错误；调用链路异常时 `openai_compat` 还有降级分支，回落到普通对话生成，保证请求尽量有可用输出。
- 详细补充：这属于“可用性优先”的落地策略，避免因单点工具故障导致整个会话失败。
- 代码锚点：`mcp_tools/client.py:898`，`core/llm/openai_compat.py:159`，`core/llm/openai_compat.py:290`

### 1.18 为什么把工具能力放在 MCP 层，而不是塞进每个 Agent 类里？
- 标准回答：工具能力集中到 MCP 后，Agent 只关心“要不要调工具”和“如何使用结果”，工具协议、进程管理、错误语义在一处维护，减少重复逻辑。
- 详细补充：本质是把“业务决策（Agent）”和“能力执行（Tool Runtime）”解耦，便于后续扩展为远端 MCP。
- 代码锚点：`core/agents/tutor.py:94`，`mcp_tools/client.py:883`，`mcp_tools/server_stdio.py:109`

### 1.19 参数配置是怎么治理的？如何避免“全靠改代码”？
- 标准回答：通用参数优先走 `.env`（检索、切块、嵌入、上下文预算）；业务约束在代码层用“按模式参数”覆盖（如 learn/practice 与 exam 的不同 top-k），形成“默认可配置 + 场景可覆盖”的分层。
- 详细补充：这种策略能兼顾可调试性和业务确定性，减少误配导致的线上行为漂移。
- 参数速记：`TOP_K_RESULTS` 是兜底；分模式优先读 `RAG_TOPK_LEARN_PRACTICE`（默认 4）和 `RAG_TOPK_EXAM`（默认 6）。
- 代码锚点：`rag/retrieve.py`，`core/orchestration/runner.py`，`rag/chunk.py`

### 1.20 课程工作区是如何做隔离的？
- 标准回答：每个课程独立目录，按 `uploads/index/notes/mistakes/exams/practices` 分子目录管理，上传、索引、笔记都在课程命名空间内运行，避免跨课程污染。
- 详细补充：这是“文件系统级租户隔离”的简化实现，足够支撑单机多课程并行。
- 代码锚点：`backend/api.py:79`，`backend/api.py:108`，`backend/api.py:115`

### 1.21 上传链路的安全底线有哪些？
- 标准回答：至少三层：课程存在校验、文件名净化（`basename` 防路径穿越）、扩展名白名单限制。这样可以防止非法路径写入和非支持格式进入解析链路。
- 详细补充：面试时可强调这属于输入边界防御，不是“可有可无”的前端校验。
- 代码锚点：`backend/api.py:163`，`backend/api.py:168`，`backend/api.py:170`

### 1.22 为什么你们把状态、引用、正文拆成三类流事件？
- 标准回答：因为三类事件语义不同：状态用于反馈进度、引用用于证据展示、正文用于最终答案。混在一个文本流里会导致渲染污染和前端解析复杂度上升。
- 详细补充：这是“协议层分离语义”的做法，能显著降低前端状态机复杂度。
- 代码锚点：`core/llm/openai_compat.py:215`，`core/orchestration/runner.py:749`，`frontend/streamlit_app.py:712`

### 1.23 前端“灰屏刷新”为什么会发生，架构上如何缓解？
- 标准回答：根因是 Streamlit 的 rerun 模型；每次交互会重跑脚本。当前通过 form 提交、`session_state` 状态持久化、`cache_data(ttl=30)` 缓存来减少无效刷新和重复请求。
- 详细补充：这是框架特性，不是单点 bug；优化目标是“降频”和“降抖动”，不是完全消灭 rerun。
- 参数速记：前端缓存 `ttl=30s`；请求超时分别为列表 `5s`、同步对话 `120s`、流式对话 `180s`、建索引 `300s`。
- 代码锚点：`frontend/streamlit_app.py:237`，`frontend/streamlit_app.py:469`，`frontend/streamlit_app.py:225`

### 1.24 你们如何做链路可观测性，定位慢点在模型还是工具？
- 标准回答：日志记录了工具轮次、请求工具名、LLM 耗时和工具耗时，并带 `via=mcp_stdio` 链路字段。看到 `llm_ms` 高就查模型侧，`elapsed_ms` 高就查工具侧。
- 详细补充：这类结构化日志对线上定位很关键，比只打“成功/失败”有用得多。
- 代码锚点：`core/llm/openai_compat.py:100`，`core/llm/openai_compat.py:135`，`core/llm/openai_compat.py:272`

### 1.25 如果要新增一个工具，最少要改哪些层？
- 标准回答：至少四层：`TOOL_SCHEMAS` 增 schema、`MCPTools._call_tool_local` 增实现分发、策略层把工具加入 allowed_tools、必要时在 prompt 里补工具使用规则。这样才会贯通“可被选中->可被执行->可被约束”。
- 详细补充：回答时可以展示“从协议到业务”的完整改动面，体现你对系统耦合点有全局认知。
- 代码锚点：`mcp_tools/client.py:21`，`mcp_tools/client.py:851`，`core/orchestration/policies.py:12`，`core/agents/tutor.py:94`

### 1.26 从当前版本走向生产，你会优先补哪些工程能力？
- 标准回答：优先级通常是：鉴权与多用户隔离、持久化与备份策略、监控告警、限流与熔断、任务异步化（索引构建）、配置分环境管理。当前代码已具备基础分层，但生产治理能力还需要补齐。
- 详细补充：这题重点不是“功能清单”，而是你能否给出有顺序的工程落地路线。
- 代码锚点：`backend/api.py:38`，`backend/api.py:265`，`memory/manager.py:186`，`mcp_tools/client.py:511`

### 1.27 你先整体介绍一下 CoursePilot，重点讲系统架构和你自己的核心贡献。
- 标准回答：CoursePilot 是“教材驱动”的课程学习 Agent 系统，主链路是 `Streamlit -> FastAPI -> OrchestrationRunner -> (Router/Tutor/QuizMaster/Grader) + RAG + MCP + Memory`。我负责了编排主链路、上下文预算与工具治理、RAG 检索策略和性能评测体系（baseline/after、checkpoint 续跑、对比报告）。
- 详细补充：面试建议用“三段式”回答：`架构落地`、`性能优化`、`工程化与可观测性`。
- 代码锚点：`core/orchestration/runner.py`，`core/orchestration/context_budgeter.py`，`core/llm/openai_compat.py`，`rag/retrieve.py`，`scripts/perf/bench_runner.py`

### 1.28 为什么这个场景要做成多 Agent，而不是单 Agent + 工具调用？
- 标准回答：因为这是“学习-出题-评卷-记忆回写”的复合流程，不是单轮问答。多 Agent 可以把规划、教学、出题、评卷拆成稳定职责，降低提示词耦合和回归风险；单 Agent 在复杂流程下更容易漂移。
- 详细补充：多 Agent 的关键不是“并行”，而是“职责边界可测试”。
- 代码锚点：`core/agents/router.py`，`core/agents/tutor.py`，`core/agents/quizmaster.py`，`core/agents/grader.py`

### 1.29 Router / Tutor / QuizMaster / Grader 各自负责什么？状态怎么传递？
- 标准回答：Router 产计划（need_rag/style/allowed_tools）；Tutor 负责学习回答；QuizMaster 负责练习出题与考试出卷；Grader 负责评卷讲解。状态通过 Runner 显式传递（mode/history/context），并通过内部元数据 `quiz_meta/exam_meta` 串联出题到评卷。
- 详细补充：强调“不是 Agent 互相聊天”，而是 Runner 统一调度与状态收敛。
- 代码锚点：`core/orchestration/runner.py`，`backend/schemas.py`

### 1.30 多 Agent 的收益是什么？有没有带来额外复杂性？
- 标准回答：收益是可维护性、可观测性、可回归性更好；复杂性是编排分支和状态管理成本增加。当前通过统一 Runner、统一工具契约、统一指标埋点来控制复杂性。
- 详细补充：可给 trade-off 结论：`用编排复杂度换业务可控性`。
- 代码锚点：`core/orchestration/runner.py`，`core/orchestration/policies.py`，`core/metrics/`

## 2. RAG（解析/切块/索引/混合检索/引用）

### 2.1 文档解析支持哪些格式？
- 标准回答：支持 PDF/TXT/MD/DOCX/PPTX/PPT。`.ppt` 先通过 COM 转 `.pptx` 再解析。解析失败按文件级容错，不阻断整批构建。
- 详细补充：可补充 `.ppt` 转换依赖 Windows COM，跨平台部署时通常建议统一为 `.pptx`。
- 代码锚点：`rag/ingest.py:170`，`rag/ingest.py:119`，`rag/ingest.py:153`

### 2.2 切块策略是什么？
- 标准回答：按字符长度切块，支持 overlap；参数来自环境变量 `CHUNK_SIZE/CHUNK_OVERLAP`。有死循环防护：`overlap >= chunk_size` 时自动调整并加 next_start 兜底。
- 详细补充：建议说明 overlap 过大会重复信息、过小会丢上下文，当前是稳定优先的字符切块策略。
- 参数速记：环境变量默认值：`CHUNK_SIZE=512`、`CHUNK_OVERLAP=50`；你当前 `.env` 配置是 `600/120`。
- 代码锚点：`rag/chunk.py:13`，`rag/chunk.py:49`，`rag/chunk.py:22`，`rag/chunk.py:35`

### 2.3 嵌入模型如何选设备和批量？
- 标准回答：`EMBEDDING_DEVICE=auto` 时优先 CUDA；模型名来自 `EMBEDDING_MODEL`；批量由 `EMBEDDING_BATCH_SIZE` 控制（GPU 默认更大）。查询向量按 BGE 规则可加前缀。
- 详细补充：可提到首轮会有模型加载开销，之后复用实例，吞吐主要受设备和 batch_size 影响。
- 参数速记：常用参数：`EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5`、`EMBEDDING_DEVICE=auto`、`EMBEDDING_BATCH_SIZE=256`。
- 代码锚点：`rag/embed.py:31`，`rag/embed.py:55`，`rag/embed.py:62`，`rag/embed.py:21`

### 2.4 向量索引如何构建与检索？
- 标准回答：使用 `FAISS IndexFlatL2`，入库时保存 chunks 元数据；检索返回距离并映射为 `score=1/(1+d)` 作为可读相关度分数。
- 详细补充：补一句当前是平面索引（IndexFlatL2），精度高但在超大库下查询成本线性增长。
- 代码锚点：`rag/store_faiss.py:27`，`rag/store_faiss.py:35`，`rag/store_faiss.py:44`

### 2.5 BM25 是怎么实现的？
- 标准回答：项目内自实现 BM25（无外部依赖），中英文混合分词；预计算 TF/IDF，查询时按 BM25 公式打分并排序。
- 详细补充：可强调 BM25 在关键词命中上补足 dense 召回，特别是术语/缩写类查询。
- 代码锚点：`rag/lexical.py:15`，`rag/lexical.py:37`，`rag/lexical.py:56`，`rag/lexical.py:58`

### 2.6 混合检索如何融合 dense + BM25？
- 标准回答：先分别召回候选，再用 RRF 融合（`HYBRID_RRF_K` + 双权重），最后截到 top_k；不是简单拼接。
- 详细补充：说明 RRF 的价值是“统一排序框架”，避免直接拼分数时不同量纲不可比。
- 参数速记：融合参数：`HYBRID_RRF_K=60`、`HYBRID_DENSE_WEIGHT=1.0`、`HYBRID_BM25_WEIGHT=1.0`（默认）。
- 代码锚点：`rag/retrieve.py:65`，`rag/retrieve.py:71`，`rag/retrieve.py:72`，`rag/retrieve.py:73`

### 2.7 top-k 参数在三模式是否一致？
- 标准回答：不一致。Runner 会按模式下发 top-k：学习/练习读 `RAG_TOPK_LEARN_PRACTICE`（默认 4），考试读 `RAG_TOPK_EXAM`（默认 6）；`TOP_K_RESULTS` 只作为兜底。
- 详细补充：回答时明确“默认值来自 .env，但业务分支可显式覆盖”，这是参数优先级问题。
- 参数速记：`RAG_TOPK_LEARN_PRACTICE=4`、`RAG_TOPK_EXAM=6`、`TOP_K_RESULTS`（fallback）。
- 代码锚点：`rag/retrieve.py`，`core/orchestration/runner.py`

### 2.8 引用是如何生成并展示的？
- 标准回答：Retriever 把 chunk 格式化为 `[来源N: doc_id, 页码]` 上下文；流式路径先发送 citations 事件，前端在消息下方渲染“查看引用来源”。
- 详细补充：补充引用来源来自 chunk 元数据（文档名/页码），不是模型自由生成。
- 代码锚点：`rag/retrieve.py:137`，`core/orchestration/runner.py:674`，`frontend/streamlit_app.py:647`

### 2.9 索引重建流程如何保证一致性？
- 标准回答：`rebuild_indexes.py` 使用与后端相同的解析/切块/建库函数，保证离线重建与在线构建逻辑一致。
- 详细补充：可强调离线重建脚本复用同一管线，避免“线上检索逻辑”和“离线建库逻辑”漂移。
- 代码锚点：`rebuild_indexes.py:22`，`rebuild_indexes.py:56`，`rebuild_indexes.py:72`，`rebuild_indexes.py:76`

### 2.10 当前 RAG 已知短板是什么？
- 标准回答：当前仍无 Cross-Encoder reranker；虽然已支持 `chapter_hybrid` 分层切分和句级压缩，但排序阶段主要依赖 dense+BM25+RRF，复杂语义排序仍有上限。
- 详细补充：建议把短板分成“召回、排序、切块”三类讲，更像工程评估而不是泛泛而谈。
- 代码锚点：`rag/chunk.py:13`，`rag/lexical.py:15`，`rag/retrieve.py:65`

### 2.11 你的 RAG 流水线具体是什么？从文档接入到最终生成答案，中间有哪些模块？
- 标准回答：完整链路是 `ingest(解析) -> chunk(切块) -> embed(向量化) -> FAISS建库 -> retrieve(召回/融合/压缩) -> format_context(引用拼装) -> Agent生成`。在线请求由 Runner 判定是否 need_rag，再将检索证据注入上下文预算器。
- 详细补充：离线建库与在线检索分层，便于独立优化与排障。
- 代码锚点：`rag/ingest.py`，`rag/chunk.py`，`rag/embed.py`，`rag/store_faiss.py`，`rag/retrieve.py`，`core/orchestration/runner.py`

### 2.12 为什么用“章节分层切块”，而不是固定长度 chunk？
- 标准回答：固定长度切块容易跨语义边界切断，章节分层先按章节/小节组织，再在章内子切块，能提升语义完整性和引用可解释性，减少噪声召回。
- 详细补充：当前默认 `chapter_hybrid`，识别失败自动回退 `fixed`，兼顾效果与稳定性。
- 参数速记：`CHUNK_STRATEGY=chapter_hybrid`（失败回退 `fixed`）。
- 代码锚点：`rag/chunk.py`

### 2.13 BM25 + 向量混合检索为什么会比单路召回更稳？
- 标准回答：向量检索擅长语义相似，BM25 擅长关键词精确命中；课程场景常有术语、符号、缩写，单路容易偏科。混合后用 RRF 按排名融合，能在不同问题类型下更稳定。
- 详细补充：这不是“简单相加”，而是统一排序框架。
- 代码锚点：`rag/lexical.py`，`rag/retrieve.py`

### 2.14 重排序你是怎么做的？为什么需要 rerank？
- 标准回答：当前线上是 `RRF rank fusion + 句级压缩筛句`，还没有启用 Cross-Encoder rerank。需要 rerank 是因为很多错误来自“召回到了但排序靠后”，二阶段精排可以提升 top 证据质量。
- 详细补充：若上 rerank，通常放在召回候选之后（例如 top12）再裁到最终 top_k。
- 代码锚点：`rag/retrieve.py`

### 2.15 如果答案错了，你怎么判断是检索错了还是生成错了？
- 标准回答：先看检索指标与证据命中（hit@k/top1_acc/precision@k）；若证据本身不相关是检索问题，若证据相关但回答偏离则是生成问题。最后结合引用文本做案例回放确认。
- 详细补充：工程上用“离线指标 + 在线case复盘”双轨定位，避免拍脑袋。
- 代码锚点：`benchmarks/rag_gold_v1.jsonl`，`scripts/perf/bench_runner.py`，`data/perf_runs/*/baseline_raw.jsonl`

### 2.16 你提到 Top-k 命中率提升 65%，这个指标是怎么定义、怎么测的？
- 标准回答：常用定义是 `hit@k=前k检索结果里至少有1条命中 gold 证据` 的样本占比。测法是固定 cases 与 gold、固定模型与参数，跑完整基准后按 overall/by_mode 聚合对比 baseline 与 after。
- 详细补充：必须强调“同口径同数据集”，否则提升数字不可比。
- 代码锚点：`benchmarks/cases_v1.jsonl`，`benchmarks/rag_gold_v1.jsonl`，`scripts/perf/bench_runner.py`

### 2.17 检索耗时降低 77% 是怎么做到的？主要优化了哪几个环节？
- 标准回答：主要来自减少无效检索成本：分模式 top-k 收敛、章节分层切块降低噪声候选、句级压缩减少后续上下文膨胀、工具链路去重降低重复检索调用。
- 详细补充：检索变快不等于总时延同比例下降，因为总体瓶颈常在 LLM 多轮推理。
- 代码锚点：`core/orchestration/runner.py`，`rag/chunk.py`，`rag/retrieve.py`，`core/llm/openai_compat.py`

### 2.18 你如何缓解中间信息丢失、长文档截断、上下文噪声？
- 标准回答：用 `ContextBudgeter` 分段治理：历史做“最近轮+摘要卡片（可选LLM压缩）”，RAG 做句级压缩，memory 只注入短片段，最后 hard truncate；并在最终回答轮 rehydrate，避免最终答复证据不足。
- 详细补充：这是“结构性去重 + 条件压缩”，不是简单暴力截断。
- 代码锚点：`core/orchestration/context_budgeter.py`，`core/llm/openai_compat.py`，`core/agents/tutor.py`

## 3. MCP（协议子集/stdio链路/错误语义/重连/可观测性）

### 3.1 工具调用主链路是什么？
- 标准回答：LLM 产生 tool_call 后，`openai_compat` 调 `MCPTools.call_tool`；该方法严格走 `_StdioMCPClient.call_tool`；server 端 `tools/call` 再落到 `_call_tool_local` 执行。
- 详细补充：可以按 4 跳链路回答：LLM tool_call -> MCP client -> stdio server -> 本地工具实现。
- 参数速记：Act 阶段工具循环上限默认 `ACT_MAX_ROUNDS=4`，之后进入 Synthesize 最终回答。
- 代码锚点：`core/llm/openai_compat.py:133`，`mcp_tools/client.py:883`，`mcp_tools/server_stdio.py:109`，`mcp_tools/client.py:851`

### 3.2 实现了哪些 MCP 协议？
- 标准回答：当前仅 tools 标准子集：`initialize`、`notifications/initialized`、`tools/list`、`tools/call`。
- 详细补充：补一句这是“tools 子集实现”，当前不覆盖 resources/prompts/streamable transport。
- 参数速记：协议版本固定 `protocolVersion=2024-11-05`。
- 代码锚点：`mcp_tools/server_stdio.py:4`，`mcp_tools/server_stdio.py:96`，`mcp_tools/server_stdio.py:106`，`mcp_tools/server_stdio.py:109`

### 3.3 工具 schema 如何从 OpenAI function 转 MCP？
- 标准回答：`_to_mcp_tools()` 从 `TOOL_SCHEMAS` 映射 `name/description/inputSchema`，避免双份 schema 分叉。
- 详细补充：强调 schema 单源化后，只需维护 TOOL_SCHEMAS 一处即可同时服务 LLM 和 MCP。
- 代码锚点：`mcp_tools/client.py:477`

### 3.4 如何证明“严格仅 MCP，无本地 fallback”？
- 标准回答：`MCPTools.call_tool()` 失败时返回 `success:false, via:mcp_stdio`，不再本地直调；测试用 missing server 场景验证了这一点。
- 详细补充：建议指出失败时返回统一错误结构而非降级执行，保证链路语义一致可观测。
- 代码锚点：`mcp_tools/client.py:883`，`mcp_tools/client.py:900`，`tests/test_mcp_stdio.py:93`

### 3.5 stdio MCP 客户端生命周期如何管理？
- 标准回答：按需拉起子进程，`_ensure_ready_locked` 中完成初始化握手；模块退出通过 `atexit` 清理。
- 详细补充：可补充懒启动减少空闲成本，atexit 清理避免遗留僵尸子进程。
- 参数速记：stdio client 请求超时 `request_timeout=20.0s`；进程异常会触发一次自动恢复流程。
- 代码锚点：`mcp_tools/client.py:281`，`mcp_tools/client.py:353`，`mcp_tools/client.py:359`，`mcp_tools/client.py:511`

### 3.6 错误码语义如何对齐 JSON-RPC？
- 标准回答：方法不存在 `-32601`，参数错误 `-32602`，服务异常 `-32000`；参数类型不对也按 `-32602` 返回。
- 详细补充：回答时顺带说“参数错误和方法缺失分码返回”，方便客户端做精确提示。
- 代码锚点：`mcp_tools/server_stdio.py:87`，`mcp_tools/server_stdio.py:113`，`mcp_tools/server_stdio.py:131`，`mcp_tools/server_stdio.py:144`

### 3.7 重连机制如何工作？
- 标准回答：`rpc/notify` 在 `_MCPTransportError` 时会重启并重试一次，降低瞬时故障影响；但有副作用工具重复执行风险（需幂等化）。
- 详细补充：重试是一把双刃剑：提升可用性，但对非幂等工具必须设计去重策略。
- 参数速记：`rpc/notify` 代码层重试上限为 `1` 次（循环 `range(2)`，首次失败后仅再试一次）。
- 代码锚点：`mcp_tools/client.py:381`，`mcp_tools/client.py:410`，`mcp_tools/client.py:420`

### 3.8 为什么要把 print 重定向到 stderr？
- 标准回答：stdio 协议要求 stdout 仅输出帧消息；任何业务日志若写入 stdout 会污染 `Content-Length` 帧并导致协议解析失败。
- 详细补充：可加一句“stdout 污染会直接破坏帧边界”，这是 stdio 协议实现最常见坑。
- 代码锚点：`mcp_tools/server_stdio.py:26`，`mcp_tools/server_stdio.py:29`，`mcp_tools/server_stdio.py:38`

### 3.9 链路可观测性是如何做的？
- 标准回答：工具执行日志记录 round/requested/executed/elapsed；工具结果默认补 `via=mcp_stdio` 字段，便于日志和前端定位调用路径。
- 详细补充：建议强调日志里有 round/requested/executed/elapsed，可直接定位慢点在 LLM 还是工具。
- 代码锚点：`core/llm/openai_compat.py:58`，`core/llm/openai_compat.py:183`，`mcp_tools/client.py:466`，`mcp_tools/client.py:894`

### 3.10 MCP 这条链路目前有哪些自动化验证？
- 标准回答：有独立测试覆盖 server 可导入、初始化、tools/list、tools/call、错误码语义、严格无 fallback。
- 详细补充：可说明测试重点是协议连通性和错误语义，而不是工具业务正确性的全覆盖。
- 参数速记：当前核心覆盖 `4` 个协议方法（`initialize/notifications/initialized/tools/list/tools/call`）+ `2` 类关键错误码（`-32601/-32602`）。
- 代码锚点：`tests/test_mcp_stdio.py:15`，`tests/test_mcp_stdio.py:63`，`tests/test_mcp_stdio.py:80`，`tests/test_mcp_stdio.py:93`

### 3.11 你封装了 6 类 MCP 工具，MCP 在你的系统里解决了什么核心问题？
- 标准回答：MCP 解决的是“工具调用标准化”问题：统一 schema、统一调用协议、统一错误语义、统一日志链路。这样 Agent 只做决策，工具运行时治理收敛在一层。
- 详细补充：这让工具可替换性更强，后续扩展远端 MCP 成本更低。
- 代码锚点：`mcp_tools/client.py`，`mcp_tools/server_stdio.py`，`core/llm/openai_compat.py`

### 3.12 工具调用失败、超时、结果格式错误时你怎么处理？
- 标准回答：调用前做 preflight（phase/参数/策略）；调用中记录结构化事件并分类为 `success/retryable_error/fatal_error`；按契约进行有限重试或直接降级到 Synthesize，保证请求可用。
- 详细补充：重点是“可观测 + 可降级”，不是把异常静默吞掉。
- 代码锚点：`core/orchestration/policies.py`，`core/llm/openai_compat.py`

### 3.13 如何避免 Agent 滥用工具，或者循环调用工具？
- 标准回答：通过统一门控 + 去重 + 轮次上限：Act 阶段才允许工具、参数必填校验、request 级签名去重、`memory_search` 按意图受限、超轮次强制进入 Synthesize。
- 详细补充：这是架构内约束，不是单工具打补丁。
- 参数速记：`ACT_MAX_ROUNDS`、`TOOL_RETRY_MAX`、`MEMORY_DEDUP_*`、`MEMORY_SEARCH_IN_ACT_DEFAULT`。
- 代码锚点：`core/orchestration/policies.py`，`core/llm/openai_compat.py`

### 3.14 Function Calling 和 MCP 的区别是什么？
- 标准回答：Function Calling 是模型侧“要调什么工具”的决策机制；MCP 是工具侧“怎么发现、调用、返回”的协议与运行时机制。前者偏决策，后者偏执行标准化。
- 详细补充：一句话：`Function Calling 选工具，MCP 管工具`。
- 代码锚点：`core/llm/openai_compat.py`，`mcp_tools/client.py`，`mcp_tools/server_stdio.py`

### 3.15 为什么不直接写普通 Python tool，而要强调 MCP Server 统一管理？
- 标准回答：普通 Python 直调虽然快，但容易形成多处分散实现、错误语义不一致、观测难。MCP 统一管理后，schema/协议/日志/重试都能收敛，工程可维护性更好。
- 详细补充：这属于从“函数集合”升级到“可治理工具层”。
- 代码锚点：`mcp_tools/client.py`，`mcp_tools/server_stdio.py`

## 4. 记忆系统（情景记忆/用户画像/触发条件/检索注入）

### 4.1 情景记忆分几种事件类型？
- 标准回答：`episodes.event_type` 设计为 `qa / mistake / practice / exam`。
- 详细补充：回答时把四类事件映射到业务场景：学习问答、练习错题、练习正常、考试记录。
- 参数速记：事件类型固定 `4` 类：`qa`、`mistake`、`practice`、`exam`。
- 代码锚点：`memory/store.py:41`

### 4.2 学习模式问答什么时候写入记忆？
- 标准回答：当前在 `run_learn_mode_stream` 完成后写入 `qa`，并记录 `doc_ids` 到 metadata，同时通过统一入口 `record_event` 完成 `total_qa + 1`。
- 详细补充：补充学习模式写入时会带 doc_ids，后续检索能回到学习材料上下文。
- 参数速记：学习模式记忆写入参数：`event_type=qa`、`importance=0.5`、`increment_qa=True`，`doc_ids` 去重后写入 metadata。
- 代码锚点：`core/orchestration/runner.py:723`，`core/orchestration/runner.py:771`，`memory/manager.py:191`

### 4.3 练习模式什么时候写入记忆？
- 标准回答：先通过 `_is_answer_submission` 判断用户是否在提交答案，进入评分后调用 `_save_grading_to_memory`，再统一走 `record_event` 写入。
- 详细补充：关键是先识别“是否作答”再评分入库，避免普通闲聊误写成练习记录。
- 参数速记：作答识别窗口看最近 `12` 条历史（`history[-12:]`）；命中后才进入评分写入链路。
- 代码锚点：`core/orchestration/runner.py:429`，`core/orchestration/runner.py:477`，`core/orchestration/runner.py:511`

### 4.4 练习题“正确答案”会不会存入？
- 标准回答：会。评分后分数 >=60 写 `practice`（importance 0.4），<60 写 `mistake`（importance 0.9）。不是只存错题。
- 详细补充：可强调系统不是“只记错题”，正确样本也保存，用于刻画学习轨迹。
- 参数速记：判定阈值：分数 `>=60` 记 `practice`（`importance=0.4`），`<60` 记 `mistake`（`importance=0.9`）。
- 代码锚点：`backend/schemas.py:158`，`core/orchestration/runner.py:503`，`core/orchestration/runner.py:505`

### 4.5 考试模式记忆现在是否已写入？
- 标准回答：已修复。考试批改命中后会调用 `_save_exam_to_memory`，并通过 `record_event` 写 `event_type=exam`，同步薄弱点与知识点掌握度到画像。
- 详细补充：建议补一句考试记录会同步画像薄弱点，形成“评估 -> 画像更新”的闭环。
- 参数速记：考试记忆常用参数：`weak_points` 截到前 `8` 个，`importance` 按分数分支（低分更高）。
- 代码锚点：`core/orchestration/runner.py:300`，`core/orchestration/runner.py:359`，`core/orchestration/runner.py:627`，`core/orchestration/runner.py:676`

### 4.6 情景记忆存储结构是什么？
- 标准回答：SQLite `episodes` 表，字段含 `content`（自然语言摘要）、`importance`、`created_at`、`metadata(JSON)`，支持按课程和类型过滤索引。
- 详细补充：回答时突出 content 与 metadata 分离：前者便于检索展示，后者便于结构化扩展。
- 代码锚点：`memory/store.py:37`，`memory/store.py:45`，`memory/store.py:52`

### 4.7 会把整段对话原文全量入库吗？
- 标准回答：不会。存的是事件摘要（如“问题+来源”“题目+学生答案+得分+标签”），metadata 存结构化附加信息（score/tags/doc_ids）。
- 详细补充：可补充这是隐私与成本取舍：不全量存原文可降低存储压力和提示词泄漏风险。
- 代码锚点：`memory/store.py:42`，`core/orchestration/runner.py:492`，`core/orchestration/runner.py:506`，`core/orchestration/runner.py:769`

### 4.8 情景记忆如何检索？
- 标准回答：`search_episodes` 采用关键词 LIKE（分词 OR）、可选事件类型过滤、按 `importance DESC + created_at DESC` 排序，取 `top_k`。
- 详细补充：说明当前检索是关键词 LIKE 方案，优点是简单稳定，缺点是语义能力有限。
- 参数速记：记忆检索默认 `top_k=5`（`memory_search` 工具层），再按重要度和时间排序。
- 代码锚点：`memory/store.py:93`，`memory/store.py:111`，`memory/store.py:120`，`memory/store.py:130`

### 4.9 什么情况下会触发检索？会不会把整条记录全塞模型？
- 标准回答：练习/考试路径会通过 `_fetch_history_ctx -> memory_search` 预取历史；注入时只取前 2 条、每条截断 120 字，不会把全库全量注入。
- 详细补充：建议强调检索结果进入模型前再次压缩，防止记忆上下文反客为主。
- 参数速记：注入模型前再压缩：只取前 `2` 条历史，每条最多 `120` 字；避免长上下文污染。
- 代码锚点：`core/orchestration/runner.py:142`，`core/orchestration/runner.py:400`，`core/orchestration/runner.py:406`，`core/orchestration/runner.py:419`

### 4.10 用户画像存储结构是什么？
- 标准回答：`user_profiles` 以 `(user_id, course_name)` 为主键，字段含 `weak_points(JSON list)`、`concept_mastery(JSON dict)`、`pref_style`、`total_qa`、`total_practice`、`avg_score`。
- 详细补充：可补充画像是“课程级别聚合”，不是全局用户画像，隔离粒度更细。
- 参数速记：`concept_mastery` 结构为 `{知识点: {mastery(0~1), attempts, avg_score}}`；`pref_style` 当前默认值是 `step_by_step`。
- 代码锚点：`memory/store.py:55`，`memory/store.py:58`，`memory/store.py:205`

### 4.11 用户画像如何更新？
- 标准回答：现在统一由 `record_event` 处理：写情景记忆后，同步更新 `total_qa/total_practice/avg_score`、`weak_points`，并按分数更新 `concept_mastery`。
- 详细补充：回答时点明更新策略是合并去重并截断，防止 weak_points 无限膨胀。
- 参数速记：画像 `weak_points` 上限 `20`；渲染展示时通常只展示前 `8`。
- 代码锚点：`memory/manager.py:136`，`memory/manager.py:191`，`memory/manager.py:239`，`core/orchestration/runner.py:511`

### 4.12 用户画像在模型侧怎么使用？
- 标准回答：Router/Tutor/Grader 都会读取 `get_profile_context` 注入提示词；注入内容是摘要句，不是整行原始画像。
- 详细补充：可强调画像主要用于提示词增强，不直接参与评分逻辑，职责上与 grader 分离。
- 参数速记：注入时 `weak_points` 仅展示前 `8` 个；`concept_mastery` 仅展示“尝试次数 >=2 且掌握度最低”的前 `3` 个知识点。
- 代码锚点：`memory/manager.py:251`，`memory/manager.py:256`，`memory/manager.py:266`，`core/agents/tutor.py:76`

### 4.13 短期记忆和长期记忆分别存什么？
- 标准回答：短期记忆是当前请求/会话的工作上下文（最近历史、RAG证据、最近工具结果、当前任务状态）；长期记忆是持久化在 `memory.db` 的学习事件（qa/practice/mistake/exam）和用户画像聚合。
- 详细补充：一句话：短期保证“这次答好”，长期保证“下次更懂你”。
- 代码锚点：`core/orchestration/context_budgeter.py`，`memory/store.py`，`memory/manager.py`

### 4.14 为什么长期记忆用 SQLite？不用向量库或者图数据库？
- 标准回答：当前记忆场景以结构化筛选和聚合统计为主（按课程、事件类型、分数、时间），SQLite 轻量、事务可靠、运维成本低，和单机课程助手的规模匹配。向量库/图数据库更适合大规模语义召回或复杂关系推理。
- 详细补充：这是阶段性工程取舍，不是技术绝对优劣。
- 代码锚点：`memory/store.py`，`memory/manager.py`

### 4.15 用户画像和情景记忆是怎么设计的？什么时候注入上下文？
- 标准回答：情景记忆按事件写入（qa/practice/mistake/exam），用户画像由事件增量更新。注入时机由 Runner 按 mode/agent/phase 决定：学习偏讲解注入，练习/考试偏出题与评卷注入。
- 详细补充：注入是“按需检索+限长”，不是全量回灌。
- 代码锚点：`core/orchestration/runner.py`，`memory/manager.py`

### 4.16 如果 memory 太长，全部塞给模型会有什么问题？你怎么压缩？
- 标准回答：全量注入会导致 token 膨胀、相关性稀释、推理时延上升。当前通过 event_type 过滤、top_k 限制、单条截断、预算器统一裁剪来控制。
- 详细补充：原则是“高价值短片段优先”，而不是“越多越好”。
- 参数速记：`CB_MEMORY_MAX_TOKENS`、`CB_MEMORY_TOPK`、`CB_MEMORY_ITEM_MAX_CHARS`。
- 代码锚点：`core/orchestration/context_budgeter.py`，`core/orchestration/runner.py`

### 4.17 你的上下文策略是什么？不同 agent 的上下文为什么要差异化？
- 标准回答：统一框架是 `history -> rag -> memory -> hard_truncate`，但按 Agent 目标差异化注入：Tutor 重讲解证据，QuizMaster 重出题约束与范围，Grader 重题面/标准答案/学生答案对齐。
- 详细补充：差异化的本质是“同预算下最大化任务相关信息密度”。
- 代码锚点：`core/orchestration/runner.py`，`core/orchestration/context_budgeter.py`，`core/agents/tutor.py`，`core/agents/quizmaster.py`，`core/agents/grader.py`

## 5. 延伸追问（按上面四类补充）

### 5.1 整体项目（补充说明）
- 本轮按既定数量不新增整体项目延伸题，保持“整体项目 10 条”。

### 5.2 RAG 延伸追问（5）

### 5.3 你怎么做 RAG 召回评测，指标怎么定？
- 标准回答：离线构造 query->relevant_chunks 标注集，按模式统计 Recall@K/Precision@K/HitRate@K；先对比 dense/bm25/hybrid，再看 top_k 和融合参数敏感性。
- 详细补充：建议先定义评测集（问题->标准证据），再分别看 Recall@k、MRR、最终答案命中率。
- 代码锚点：`rag/retrieve.py:104`，`rag/retrieve.py:108`，`rag/retrieve.py:113`
- 面试高频反问：如果 hybrid 没提升，你先排查哪三件事？

### 5.4 BM25 中文单字分词会不会有噪声？
- 标准回答：会有噪声风险，尤其短 query。当前是轻量实现的工程折中，后续可升级词法分词或引入 query 重写。
- 详细补充：可补充单字切分对中文短查询有效，但噪声高时需结合停用词或最小词长策略。
- 代码锚点：`rag/lexical.py:15`，`rag/lexical.py:58`
- 面试高频反问：为什么没直接上外部分词器？

### 5.5 混合检索参数有哪些可调？
- 标准回答：可调 `HYBRID_RRF_K`、dense/bm25 权重、两路候选倍数；这些参数共同决定“召回广度”和“排序偏好”。
- 详细补充：回答时建议给出调参顺序：先 RRF_K，再两路权重，最后再调候选池大小。
- 参数速记：默认值分别为 `HYBRID_RRF_K=60`、`HYBRID_DENSE_WEIGHT=1.0`、`HYBRID_BM25_WEIGHT=1.0`。
- 代码锚点：`rag/retrieve.py:71`，`rag/retrieve.py:72`，`rag/retrieve.py:73`，`rag/retrieve.py:117`
- 面试高频反问：你如何做参数寻优而不是拍脑袋？

### 5.6 为什么考试模式 top_k 默认更高（当前是 6）？
- 标准回答：考试场景需要更广的证据覆盖，Runner 会给 exam 使用更高的 top-k（默认 6，高于 learn/practice 的 4）；这是业务策略覆盖默认参数的例子。
- 详细补充：可以解释为考试模式偏“覆盖率优先”，宁可上下文更长也要减少漏召回。
- 参数速记：`RAG_TOPK_LEARN_PRACTICE=4`、`RAG_TOPK_EXAM=6`（均可通过环境变量调整）。
- 代码锚点：`core/orchestration/runner.py`
- 面试高频反问：top_k 过大带来的负面影响是什么？

### 5.7 什么时候需要引入 reranker？
- 标准回答：当 top-k 里“有召回但排序不理想”频繁出现时，reranker 能显著提升前几位精度。当前链路仍以 RRF 排序为主，Cross-Encoder reranker 仍是后续提升空间。
- 详细补充：当你发现“相关块已召回但排序靠后”时，就是引入 reranker 的典型信号。
- 代码锚点：`rag/retrieve.py:65`，`rag/retrieve.py:137`
- 面试高频反问：你会把 reranker 放在 dense 前还是后？

### 5.8 MCP 延伸追问（5）

### 5.9 你们 RPC 的并发模型是什么，乱序响应会怎样？
- 标准回答：当前客户端是串行锁模型，单请求 in-flight，`id` 匹配是兜底。若未来并发化，需要引入 pending-map 管理多请求响应匹配。
- 详细补充：当前串行 in-flight 简化并发问题，但吞吐受限；并发化要加 pending-id 路由。
- 代码锚点：`mcp_tools/client.py:394`，`mcp_tools/client.py:410`
- 面试高频反问：并发化后你如何保证线程安全？

### 5.10 `protocolVersion` 变更时如何兼容？
- 标准回答：当前是严格版本（客户端/服务端都用 `2024-11-05`），不兼容即初始化失败；后续可做“能力协商+向后兼容矩阵”。
- 详细补充：补一句版本不一致在 initialize 阶段暴露，比运行中失败更易定位。
- 参数速记：客户端与服务端都声明 `2024-11-05`；版本不一致在初始化阶段直接失败。
- 代码锚点：`mcp_tools/client.py:179`，`mcp_tools/client.py:366`，`mcp_tools/server_stdio.py:100`
- 面试高频反问：如何设计兼容测试用例？

### 5.11 为什么 `_call_tool_local` 不算 fallback？
- 标准回答：它位于 server 端，承接 `tools/call` 的执行层；应用侧已不再本地直调。路径语义是“远程（子进程）执行”，不是“失败回退本地执行”。
- 详细补充：强调 `_call_tool_local` 是 server 执行层，不是 client 失败后的绕过路径。
- 代码锚点：`mcp_tools/server_stdio.py:117`，`mcp_tools/client.py:883`，`mcp_tools/client.py:851`
- 面试高频反问：如果将来有远程 MCP server，这层怎么抽象？

### 5.12 `tools/call` 重试会不会造成副作用重复？
- 标准回答：会有风险，尤其 filewriter append 场景。当前通过“重试一次”提升可用性，但幂等保障需要工具层补 request-id 去重。
- 详细补充：建议举 filewriter 例子：重试可能追加两次，因此必须引入幂等键。
- 参数速记：`tools/call` 传输故障最多自动重试 `1` 次；有副作用工具需做幂等防重。
- 代码锚点：`mcp_tools/client.py:410`，`mcp_tools/client.py:420`，`mcp_tools/client.py:430`
- 面试高频反问：你会在 client 还是 server 做幂等控制？

### 5.13 为什么要显式打 `via=mcp_stdio`？
- 标准回答：这是链路审计字段，能快速证明调用经过 MCP；日志聚合后可用于统计工具成功率和故障定位。
- 详细补充：可补充 `via` 字段能在日志聚合中快速做“链路占比”和“故障分层”统计。
- 代码锚点：`mcp_tools/client.py:466`，`mcp_tools/client.py:894`，`tests/test_mcp_stdio.py:88`
- 面试高频反问：除了 via，你还会补哪些 trace 字段？

### 5.14 记忆系统延伸追问（5）

### 5.15 `memory_search` 返回结构为什么要从字符串改成对象？
- 标准回答：对象结构可同时保留 `content/summary/metadata`，避免下游依赖字符串解析；并兼容前后端不同展示需求。
- 详细补充：对象化返回让前端展示和下游拼装更稳定，减少“字符串协议”带来的兼容负担。
- 代码锚点：`mcp_tools/client.py:823`，`mcp_tools/client.py:829`，`core/orchestration/runner.py:408`
- 面试高频反问：如何保证旧调用方不崩？

### 5.16 `weak_points` 为什么最多保留 20？
- 标准回答：避免画像无限膨胀导致提示词污染；同时保持“最近错误优先”的学习引导策略。
- 详细补充：上限控制的本质是 prompt 预算管理，避免用户画像反向拉高上下文成本。
- 参数速记：`weak_points` 保留上限是 `20`，超过后按“新优先”截断。
- 代码锚点：`memory/manager.py:136`，`memory/manager.py:285`
- 面试高频反问：20 这个阈值如何数据驱动优化？

### 5.17 importance 目前是固定打分吗？会衰减吗？
- 标准回答：当前是规则赋值（错题高、普通低），未做时间衰减。排序用 `importance + created_at`，是可解释但偏静态的策略。
- 详细补充：可以坦诚当前是规则分，不是学习型权重；优点可解释，缺点适应性一般。
- 参数速记：当前规则分值：练习错题常用 `0.9`，练习正确 `0.4`，学习问答约 `0.5`，考试按分数分支 `0.9/0.6`。
- 代码锚点：`core/orchestration/runner.py:505`，`core/orchestration/runner.py:674`，`memory/store.py:130`
- 面试高频反问：你会怎么设计可学习的 importance？

### 5.18 学习模式非流式为什么不写 `qa` 记忆？
- 标准回答：当前仅流式路径实现了写入，这是实现范围选择，不是模型限制。若要统一语义，需在非流式 learn 结束后补同样写入逻辑。
- 详细补充：回答时可直接说这是实现覆盖差异，后续可在非流式 learn 完成后补同样写入。
- 代码锚点：`core/orchestration/runner.py:65`，`core/orchestration/runner.py:721`
- 面试高频反问：补齐后如何避免重复写入？

### 5.19 多用户隔离如何保证？
- 标准回答：存储层按 `user_id + course_name` 设计，但当前 manager 默认 `user_id="default"` 单例复用，真实多用户需在请求入口透传 user_id。
- 详细补充：关键风险是默认 user_id 会把多用户数据混在一起，生产必须从入口透传。
- 参数速记：当前默认 `user_id="default"` 且全局 `_default_manager` 为单例 `1` 份；多用户场景必须在入口显式透传 user_id。
- 代码锚点：`memory/store.py:64`，`memory/manager.py:331`
- 面试高频反问：如果要支持租户隔离，你会改哪一层？

### 5.20 前后端基础拷打（18）

### 5.21 这个项目里前端和后端分别负责什么？
- 标准回答：前端（Streamlit）是“交互层”，负责用户操作和可视化体验，包括课程选择、模式切换、文件上传、流式回答展示、引用展示和导图展示。后端（FastAPI）是“服务层”，负责状态管理与业务执行，包括工作区生命周期、文档解析、索引构建、编排入口、SSE 推流和错误语义统一。简单说，前端负责“看见什么、怎么点”，后端负责“事情怎么做、怎么稳定做”。
- 详细补充：建议一句话区分：前端负责交互体验，后端负责稳定执行与数据生命周期。
- 代码锚点：`frontend/streamlit_app.py:456`，`frontend/streamlit_app.py:683`，`backend/api.py:265`，`backend/api.py:335`

### 5.22 能不能不要后端，让前端直接调 core？
- 标准回答：理论上可以做本地 Demo，但工程上不建议。因为上传文件、构建索引、流式协议输出、统一异常处理、本地磁盘操作这些都属于服务职责，放到前端脚本会导致 UI 进程过重、耦合高、调试和扩展都变差。保留后端的核心价值是把“可复用的业务能力”从“页面渲染逻辑”中解耦出来。
- 详细补充：可补充“前端直连 core”常见问题是权限边界缺失、异常语义不统一、复用性差。
- 代码锚点：`backend/api.py:150`，`backend/api.py:265`，`backend/api.py:355`

### 5.23 FastAPI 在这个项目里起什么作用？
- 标准回答：FastAPI 提供了这个项目的标准服务壳：HTTP 路由分发、请求/响应模型校验、状态码和异常语义、流式响应封装。它把 core 编排能力包装成稳定 API，对前端来说就是统一的调用入口。这样做的好处是后续换前端或接入其他客户端时，后端接口层基本可以复用。
- 详细补充：回答时强调 FastAPI 给了类型化接口和统一异常模型，适合作为编排系统外壳。
- 代码锚点：`backend/api.py:13`，`backend/api.py:34`，`backend/api.py:98`

### 5.24 Streamlit 在这个项目里起什么作用？
- 标准回答：Streamlit 是快速交互前端框架，适合把 AI 工作流快速产品化。这里它承载了课程与模式操作、聊天输入输出、流式文本渲染、引用面板、导图展示等 UI 能力。它的优势是开发快、状态管理简单；代价是 rerun 模型带来的刷新感，需要额外做缓存和状态细分优化。
- 详细补充：可补充 Streamlit 适合内部工具和教学场景，若追求复杂交互再考虑前后端分离框架。
- 代码锚点：`frontend/streamlit_app.py:218`，`frontend/streamlit_app.py:643`，`frontend/streamlit_app.py:727`

### 5.25 后端核心接口有哪些？
- 标准回答：核心接口可以分为五组：工作区 CRUD、文件上传与列表、索引构建与删除、同步对话 `/chat`、流式对话 `/chat/stream`。其中 `/chat` 偏“请求-响应”，`/chat/stream` 偏“边生成边返回”的交互体验。接口分层清晰后，前端调用可以按“管理类操作”和“对话类操作”分开处理。
- 详细补充：建议按“管理接口 vs 对话接口”分组回答，结构更清晰也更像工程设计。
- 代码锚点：`backend/api.py:107`，`backend/api.py:150`，`backend/api.py:265`，`backend/api.py:334`，`backend/api.py:355`

### 5.26 请求参数是怎么做结构化校验的？
- 标准回答：主要通过 Pydantic 模型定义契约，然后由 FastAPI 在入口自动校验。比如 `ChatRequest` 限定 `course_name/mode/message/history` 结构，`ChatResponse` 约束返回格式。好处是接口边界清晰、错误更早暴露、前后端联调时更容易定位字段问题。
- 详细补充：可强调 schema 校验把错误前置到接口层，避免坏数据进入核心编排流程。
- 代码锚点：`backend/schemas.py:14`，`backend/schemas.py:78`，`backend/schemas.py:86`，`backend/api.py:334`

### 5.27 为什么要配 CORS？
- 标准回答：CORS 是浏览器安全策略下的跨域放行机制。开发阶段前后端端口不同、部署阶段域名不同都可能触发跨域限制，所以后端要显式声明允许策略。即使本机可跑，提前配置 CORS 可以减少环境切换时的“线上能跑/本地报跨域”问题。
- 详细补充：补一句当前即使同机开发也建议保留 CORS 配置，减少部署切换风险。
- 代码锚点：`backend/api.py:15`，`backend/api.py:37`

### 5.28 流式输出（SSE）是怎么实现的？
- 标准回答：后端在 `chat_stream` 中迭代 Runner 的输出，把每个 chunk 包装成 SSE `data:` 行并持续推送；前端建立流式请求后逐行消费，再交给 `st.write_stream` 渲染。这样用户不用等完整答案，可以先看到首段输出与进度状态。工程上等价于把一次大响应拆成连续小响应，显著改善体感延迟。
- 详细补充：回答时点明 SSE 优化的是首字节体验（TTFB 感知），不是总生成时延本身。
- 参数速记：前端流式请求超时参数是 `timeout=180s`；后端持续发送 SSE chunk。
- 代码锚点：`backend/api.py:355`，`backend/api.py:364`，`backend/api.py:383`，`frontend/streamlit_app.py:377`，`frontend/streamlit_app.py:727`

### 5.29 前端怎么处理“状态事件”和“正文事件”？
- 标准回答：前端把流事件分成三类：正文文本、状态事件、引用事件。状态事件（`__status__`）只用于提示“正在检索/调用工具”，不进入最终回答；引用事件（`__citations__`）先缓存，等本轮结束再绑定到当前 assistant 消息。这个分流设计避免了“字典串进正文”或“引用串台”的常见问题。
- 详细补充：可强调状态/引用/正文三路分流是为了解决“可观测性”和“输出纯净性”的冲突。
- 代码锚点：`frontend/streamlit_app.py:712`，`frontend/streamlit_app.py:717`，`frontend/streamlit_app.py:732`

### 5.30 `st.session_state` 在这里解决了什么问题？
- 标准回答：Streamlit 的执行模型是每次交互都重跑脚本，所以如果不用 `session_state`，聊天历史和页面状态会频繁丢失。当前把课程、模式、历史消息、临时引用等都放进 `session_state`，确保 rerun 后还能恢复上下文。它本质上是这个前端的“会话内状态容器”。
- 详细补充：建议说明 session_state 是 Streamlit 下维持会话一致性的核心机制。
- 代码锚点：`frontend/streamlit_app.py:225`，`frontend/streamlit_app.py:229`，`frontend/streamlit_app.py:777`

### 5.31 `@st.cache_data` 的作用是什么？
- 标准回答：`@st.cache_data` 用于缓存读多写少的数据，降低重复请求开销和页面抖动。这里主要缓存课程列表和文件状态，避免每次 rerun 都发网络请求。写操作后手动 `clear()`，能兼顾“性能”与“数据新鲜度”。
- 详细补充：可以补充缓存要配合失效策略，否则会出现“数据新鲜度不足”的副作用。
- 参数速记：缓存参数：`@st.cache_data(ttl=30)`，写操作后会手动 `clear()`。
- 代码锚点：`frontend/streamlit_app.py:237`，`frontend/streamlit_app.py:249`，`frontend/streamlit_app.py:261`

### 5.32 上传文件这条链路怎么走？
- 标准回答：链路是“前端选择文件 -> POST 上传 -> 后端校验并落盘 -> 更新工作区状态”。后端会做三层保护：课程存在校验、文件名安全校验（防路径穿越）、扩展名白名单校验。通过后写入课程 `uploads` 目录，再同步到工作区文档记录。
- 详细补充：重点说三层安全校验（课程存在、文件名安全、扩展名白名单）是上传链路底线。
- 代码锚点：`frontend/streamlit_app.py:523`，`frontend/streamlit_app.py:303`，`backend/api.py:150`，`backend/api.py:165`，`backend/api.py:171`

### 5.33 构建索引是前端做还是后端做？
- 标准回答：索引构建完全在后端执行，前端只负责触发和展示结果。后端流水线是“解析 -> 切块 -> 向量化 -> 建 FAISS -> 保存”，这是 CPU/GPU 和 I/O 都较重的任务，放后端更符合职责分层。前端只需感知进度与结果，不参与重计算。
- 详细补充：可补充构建索引属于重任务，放后端便于控制资源和做超时/重试策略。
- 参数速记：构建索引调用前端等待超时 `timeout=300s`（首次下载模型时更稳妥）。
- 代码锚点：`frontend/streamlit_app.py:317`，`backend/api.py:265`，`backend/api.py:302`，`backend/api.py:312`

### 5.34 出错时前后端如何处理？
- 标准回答：后端在异常点统一抛 `HTTPException`，返回明确状态码和 `detail`，保证客户端可判读。前端收到非 200 响应会优先显示后端 detail；对超时、网络异常等也有本地兜底提示。这样可以把“业务错误”和“传输错误”区分开，便于定位。
- 详细补充：回答时建议区分“业务异常（4xx/5xx）”和“网络异常（超时/断连）”两类。
- 参数速记：同步聊天接口前端请求超时约 `timeout=120s`，并区分业务错误与网络错误。
- 代码锚点：`backend/api.py:111`，`backend/api.py:329`，`frontend/streamlit_app.py:292`，`frontend/streamlit_app.py:330`

### 5.35 模式切换后为什么行为会变化？
- 标准回答：模式切换不是只换 UI 文案，而是换后端执行流程。前端把 mode 传给后端后，Runner 会进入 learn/practice/exam 的不同分支，并绑定不同提示词、检索深度和后处理逻辑。所以模式变化本质是“编排策略变化”，不是“前端样式变化”。
- 详细补充：可强调 mode 不是 UI 标签，而是直接驱动不同编排分支和后处理策略。
- 代码锚点：`frontend/streamlit_app.py:504`，`frontend/streamlit_app.py:515`，`core/orchestration/runner.py:702`，`core/orchestration/runner.py:721`

### 5.36 聊天历史是如何传递到后端的？
- 标准回答：前端先裁剪最近若干轮历史，再构造成 `role/content` 的轻量 payload 发送给后端。后端把 `ChatRequest.history` 传给 Runner，Runner 再交给 `ContextBudgeter` 做分段预算（history/rag/memory），Tutor 默认不重复注入原始历史消息。这个设计避免历史无限膨胀。
- 详细补充：补充历史先以轻量结构跨层传递，再在 agent 层决定最终注入粒度。
- 参数速记：历史链路关键值：前端裁剪 `20`；后端常用 `CB_HISTORY_RECENT_TURNS=6`、`CB_RECENT_RAW_TURNS=3`。
- 代码锚点：`frontend/streamlit_app.py:343`，`frontend/streamlit_app.py:381`，`backend/api.py:346`

### 5.37 为什么这个前端“经常刷新”，有没有做优化？
- 标准回答：根因是 Streamlit 的 rerun 机制：按钮、输入、状态变化都会触发整页脚本重跑。当前已做三类优化：用 `form` 降低无效重跑、用缓存减少重复请求、用事件分流减少流式期间的 UI 抖动。它不能完全消除刷新，但可以明显降低“灰屏感”和卡顿感。
- 详细补充：建议指出刷新不可完全消除，只能通过 form、缓存和状态拆分降低体感。
- 代码锚点：`frontend/streamlit_app.py:467`，`frontend/streamlit_app.py:237`，`frontend/streamlit_app.py:699`

### 5.38 后端怎么启动，开发态有什么特点？
- 标准回答：后端入口是模块方式启动 `python -m backend.api`，内部由 Uvicorn 托管 ASGI 应用。当前配置 `reload=True`，开发态修改代码会自动重启服务，联调效率高。上线时通常关闭 reload，并交给进程管理器统一托管。
- 详细补充：可补充 reload 仅适合开发环境，生产应关闭并由进程管理器托管。
- 参数速记：后端默认端口 `API_PORT=8000`；开发态 `reload=True(1)`，生产建议 `reload=False(0)`。
- 代码锚点：`backend/api.py:393`，`backend/api.py:399`

### 5.39 SSE 流式输出是怎么做的？为什么不用 WebSocket？
- 标准回答：后端 `/chat/stream` 持续输出 SSE `data:` 帧，前端逐帧解析正文/状态/引用并渲染。当前是典型服务端单向推送场景，SSE 比 WebSocket 更轻量、实现和运维成本更低。
- 详细补充：如果后续有双向实时协作需求，再评估 WebSocket。
- 代码锚点：`backend/api.py`，`frontend/streamlit_app.py`

### 5.40 FastAPI + Streamlit 的前后端链路怎么设计？
- 标准回答：Streamlit 负责交互（课程、模式、上传、消息渲染），FastAPI 负责服务（workspace、索引、编排、SSE）。核心业务收敛在 Runner，前端不直接承载复杂编排。
- 详细补充：这种分层让前端可替换，后端能力可复用。
- 代码锚点：`frontend/streamlit_app.py`，`backend/api.py`，`core/orchestration/runner.py`

### 5.41 你的系统瓶颈主要在哪：检索、重排、模型推理、工具调用，还是上下文构造？
- 标准回答：当前主瓶颈通常是 LLM 多轮推理（尤其工具轮中的中间调用与最终回答轮）；检索和上下文构造通常更快，但会通过 token 体积间接放大推理时延；外部工具（如 websearch）是次级不稳定源。
- 详细补充：定位时要看分层指标：`retrieval_ms / llm_ms / e2e_ms / tool_ms`。
- 代码锚点：`core/metrics/`，`core/llm/openai_compat.py`，`data/perf_runs/*/baseline_summary.json`

### 5.42 如果并发上来之后延迟飙升，你会优先改哪几个点？
- 标准回答：优先顺序一般是：1) 先减无效工具轮与重复调用；2) 强化上下文预算控制 token；3) 把重任务异步化（索引构建等）；4) 做连接池/限流/超时治理；5) 再做水平扩展。
- 详细补充：核心原则是“先优化最重路径，再扩容”，避免把低效链路放大。
- 代码锚点：`core/llm/openai_compat.py`，`core/orchestration/context_budgeter.py`，`backend/api.py`，`scripts/perf/bench_runner.py`

## 6. 追加八股（Q91-Q130）

### 6.1 HTTP 的 GET 和 POST 核心区别是什么？
- 标准回答：GET 通常用于读取资源，语义上应无副作用；POST 通常用于创建或触发处理，可能产生副作用。
- 详细补充：面试里要强调“语义约定”比“能不能带参数”更关键。

### 6.2 什么是幂等（Idempotency）？
- 标准回答：同一个请求重复执行多次，系统状态结果一致，就是幂等。
- 详细补充：常见地，GET/PUT/DELETE 设计为幂等，POST 默认不幂等。

### 6.3 什么是 REST 的“无状态”？
- 标准回答：每个请求都应携带完成处理所需的信息，服务端不依赖会话内隐状态。
- 详细补充：无状态更易水平扩展，但需要客户端显式传递上下文。

### 6.4 常见 HTTP 状态码怎么区分？
- 标准回答：2xx 成功，4xx 客户端请求问题，5xx 服务端内部问题。
- 详细补充：最常见的面试组合是 `200/201/400/401/403/404/500`。

### 6.5 什么是 JSON 序列化与反序列化？
- 标准回答：序列化是把对象转成 JSON 字符串，反序列化是把 JSON 还原为内存对象。
- 详细补充：跨服务通信几乎都依赖这一过程，类型不一致会导致解析失败。

### 6.6 同步 I/O 和异步 I/O 的区别？
- 标准回答：同步 I/O 会阻塞当前执行流；异步 I/O 在等待期间可让出执行权处理其他任务。
- 详细补充：异步更适合高并发 I/O 场景，不等于所有场景都更快。

### 6.7 什么是事件循环（Event Loop）？
- 标准回答：事件循环是异步运行时的调度核心，负责管理任务、等待 I/O、恢复协程执行。
- 详细补充：`async/await` 的底层执行依赖事件循环调度。

### 6.8 FastAPI 相比传统 Web 框架的优势是什么？
- 标准回答：类型标注友好、自动校验、自动生成 OpenAPI 文档、异步支持完善。
- 详细补充：它特别适合“接口多、模型校验重”的服务型项目。

### 6.9 Pydantic 主要解决什么问题？
- 标准回答：做数据模型定义、类型校验、默认值管理和序列化输出。
- 详细补充：它把“输入契约”从文档约定升级为代码约束。

### 6.10 接口校验和业务校验有什么区别？
- 标准回答：接口校验验证字段类型/格式；业务校验验证业务规则是否成立。
- 详细补充：比如“字段是 int”是接口校验，“分数必须 0~100”是业务校验。

### 6.11 什么是依赖注入（Dependency Injection）？
- 标准回答：把对象创建和使用解耦，通过注入提供依赖，减少硬编码耦合。
- 详细补充：在 Web 框架里常用于注入数据库连接、鉴权上下文、配置对象。

### 6.12 Middleware（中间件）是什么？
- 标准回答：中间件是请求到达路由前和响应返回前可统一处理的拦截层。
- 详细补充：典型用途包括日志、鉴权、CORS、限流、统一异常处理。

### 6.13 CORS 是什么，为什么浏览器里常见？
- 标准回答：CORS 是浏览器跨域资源访问控制机制，用于限制网页脚本访问不同源接口。
- 详细补充：服务端要显式声明允许源、方法、头，前端才能跨域调用。

### 6.14 SSE 和 WebSocket 有什么区别？
- 标准回答：SSE 是服务端单向推送（HTTP 长连接），WebSocket 是双向全双工通信。
- 详细补充：仅需“服务端持续输出文本”时，SSE 更轻量。

### 6.15 SSE 的基本数据帧格式是什么？
- 标准回答：每条消息通常以 `data: ...` 开头，以空行分隔事件边界。
- 详细补充：格式错误会导致前端事件流解析中断。

### 6.16 为什么网络请求要设置超时？
- 标准回答：防止请求无限等待占用资源，并让调用方可及时降级或重试。
- 详细补充：生产里应区分连接超时和读取超时。

### 6.17 什么场景适合做重试？什么场景不适合？
- 标准回答：临时性网络错误适合重试；有副作用且非幂等操作不应盲目重试。
- 详细补充：重试需配合幂等键或去重策略。

### 6.18 Streamlit 为什么会“频繁刷新”？
- 标准回答：因为 Streamlit 采用脚本 rerun 机制，交互会触发整脚本重执行。
- 详细补充：要靠 session_state、缓存、表单等方式减少无效 rerun 体感。

### 6.19 `session_state` 的作用是什么？
- 标准回答：在 Streamlit 多次 rerun 之间保存会话状态，如聊天历史、选项、临时变量。
- 详细补充：没有它，页面交互后状态很容易丢失。

### 6.20 `cache_data` 和 `cache_resource` 的区别？
- 标准回答：`cache_data` 适合可序列化数据结果；`cache_resource` 适合连接池、模型实例等资源对象。
- 详细补充：选错缓存类型会导致性能或一致性问题。

### 6.21 Python 的 GIL 是什么？
- 标准回答：GIL 是 CPython 的全局解释器锁，同一时刻只允许一个线程执行 Python 字节码。
- 详细补充：它限制 CPU 密集多线程并行，但 I/O 密集场景影响较小。

### 6.22 线程和进程的主要区别？
- 标准回答：线程共享进程内存，切换轻；进程内存隔离，稳定性高但开销更大。
- 详细补充：CPU 密集任务常用多进程，I/O 密集任务常用多线程/异步。

### 6.23 什么是 CPU 密集型 vs I/O 密集型任务？
- 标准回答：CPU 密集主要耗计算；I/O 密集主要耗等待（磁盘/网络/数据库）。
- 详细补充：这是选择并发模型（多进程/多线程/异步）的前提。

### 6.24 日志级别一般怎么划分？
- 标准回答：常见有 DEBUG、INFO、WARNING、ERROR、CRITICAL。
- 详细补充：生产环境通常默认 INFO，排障时临时提升到 DEBUG。

### 6.25 什么是结构化日志？
- 标准回答：把日志按键值对结构输出，便于检索、聚合和告警。
- 详细补充：比纯文本日志更适合 ELK、Loki 等平台分析。

### 6.26 为什么推荐用环境变量管理配置？
- 标准回答：可把配置与代码分离，便于不同环境切换，减少硬编码敏感信息。
- 详细补充：这是 12-Factor 应用方法里的核心实践之一。

### 6.27 什么是 12-Factor App（简述）？
- 标准回答：一套云原生应用方法论，强调配置外置、依赖声明、日志流化、环境一致性等。
- 详细补充：面试常考“配置外置”和“无状态进程”这两点。

### 6.28 单元测试、集成测试、端到端测试区别？
- 标准回答：单元测单个函数/类；集成测模块协作；端到端测完整业务链路。
- 详细补充：三者不是互斥关系，而是测试金字塔的不同层级。

### 6.29 Mock 的目的是什么？
- 标准回答：隔离外部依赖，让测试可控、可重复、执行快。
- 详细补充：过度 Mock 会降低测试真实性，要在边界处使用。

### 6.30 Pytest fixture 是什么？
- 标准回答：fixture 是测试前后置资源管理机制，用于构建和复用测试上下文。
- 详细补充：它能显著减少重复 setup/teardown 代码。

### 6.31 什么是向量嵌入（Embedding）？
- 标准回答：把文本等对象映射到高维向量空间，使语义相近对象在空间中更接近。
- 详细补充：Embedding 是语义检索与推荐系统的基础。

### 6.32 余弦相似度和欧氏距离有什么差别？
- 标准回答：余弦关注向量方向（语义角度），欧氏距离关注绝对距离（尺度也影响）。
- 详细补充：文本检索中常优先余弦或内积相似度。

### 6.33 什么是 ANN（近似最近邻）检索？
- 标准回答：在可接受精度损失下，用更低延迟查找近邻，适合大规模向量检索。
- 详细补充：FAISS 常用来实现 ANN，核心是以速度换少量精度。

### 6.34 BM25 的核心思想是什么？
- 标准回答：基于词频、逆文档频率和文档长度归一化，对关键词匹配相关性打分。
- 详细补充：它是经典 lexical 检索方法，对“关键词精确命中”很有效。

### 6.35 什么是混合检索（Hybrid Retrieval）？
- 标准回答：把稠密语义检索和稀疏关键词检索结合，兼顾语义召回和词面命中。
- 详细补充：常见融合方式有加权、RRF、学习排序。

### 6.36 RRF（Reciprocal Rank Fusion）为什么常用？
- 标准回答：它按排名融合而非原始分数，减少不同检索器分值不可比问题。
- 详细补充：实现简单、鲁棒性强，是工程上很实用的融合策略。

### 6.37 什么是 Reranker，什么时候需要？
- 标准回答：Reranker 对初步召回结果做二次精排，用更强模型提升前几条相关性。
- 详细补充：当“召回到了但排序不准”时最值得引入。

### 6.38 Chunk 切分为什么是 RAG 关键参数？
- 标准回答：切分影响召回粒度和上下文完整性，过大易噪声，过小易语义断裂。
- 详细补充：通常要联合 `chunk_size/overlap/top_k` 一起调。

### 6.39 什么是模型幻觉（Hallucination）？常见缓解手段有哪些？
- 标准回答：模型生成与事实不一致但看似合理的内容。常用缓解包括检索增强、引用约束、工具验证、输出模板约束。
- 详细补充：要把“生成能力”和“事实校验能力”分层设计。

### 6.40 什么是 ACID？SQLite 为什么在原型阶段常见？
- 标准回答：ACID 指原子性、一致性、隔离性、持久性；SQLite 轻量、零运维、单文件部署，适合原型和中小规模场景。
- 详细补充：它的短板是高并发写入能力有限，规模上来通常迁移到服务型数据库。


