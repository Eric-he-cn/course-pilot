#  Q&A

## 使用说明

## 整体项目（架构/多Agent/前后端/上下文）

### Q01：这个项目的主架构是什么？
- 标准回答：主架构是“前端 Streamlit + 后端 FastAPI + Core 编排器 + RAG + MCP 工具 + 记忆系统”。核心编排由 `OrchestrationRunner` 统一路由 learn/practice/exam 三个模式。
- 详细补充：可以补一句“Runner 是唯一编排入口”，强调任何模式切换最终都在编排层收敛，便于排障。
- 代码锚点：`core/orchestration/runner.py:30`，`backend/api.py:341`，`backend/api.py:366`

### Q02：Agent 之间如何“通信”？
- 标准回答：不是 Agent-to-Agent 对话网络，而是 Runner 中央编排。Runner 调 Router 产计划，再调用 Tutor/Grader/QuizMaster；工具调用统一经 LLM function-calling -> MCP。
- 详细补充：回答时点明“不是分布式消息总线”，而是函数级调用链，这样能解释为什么调试主要看 Runner 日志。
- 代码锚点：`core/orchestration/runner.py:702`，`core/orchestration/runner.py:721`，`core/llm/openai_compat.py:133`

### Q03：为什么用多个 Agent，而不是一个 Agent+多提示词？
- 标准回答：当前差异不只提示词，还包括执行路径与约束：Router 产结构化 Plan，Tutor 支持工具流式教学，Grader 强制 calculator 评分链路，QuizMaster专职出题。职责分离能降低提示词耦合和回归风险。
- 详细补充：补充“职责分离后可分别做回归测试”，避免一个大 prompt 改动影响全部能力。
- 代码锚点：`core/agents/router.py:62`，`core/agents/tutor.py:26`，`core/agents/grader.py:17`，`core/agents/quizmaster.py:73`

### Q04：多轮对话是怎么实现的？
- 标准回答：前端截取最近历史发送后端；后端将历史透传到 Runner；Tutor 在 `_build_messages` 中按 `history_limit` 注入历史消息。学习/练习默认 20，考试路径传 30。
- 详细补充：建议强调历史消息是“前端裁剪 + Tutor 再裁剪”双保险，而不是无上限拼接。
- 参数速记：`history_limit=20`（学习/练习），考试路径传 `30`；前端历史裁剪默认最近 `20` 条。
- 代码锚点：`frontend/streamlit_app.py:343`，`backend/api.py:346`，`core/agents/tutor.py:84`，`core/orchestration/runner.py:294`

### Q05：上下文过长如何处理？
- 标准回答：当前是“轮次截断”策略，不是 token 级动态预算。前端仅送最近 20 条，Tutor 只取 `history[-history_limit:]`。这是工程上简单可控，但不是最优压缩方案。
- 详细补充：可直接承认当前没有 token 级压缩器，优势是行为可预测，代价是长文本场景利用率不高。
- 参数速记：当前“token 预算器 = 0（未启用）”；轮次截断参数为前端最近 `20` 条、Tutor `history_limit=20`，考试分支 `history_limit=30`。
- 代码锚点：`frontend/streamlit_app.py:343`，`core/agents/tutor.py:35`，`core/agents/tutor.py:84`

### Q06：后端存在的核心价值是什么？
- 标准回答：后端承担课程工作区管理、上传解析、索引构建、流式 SSE 输出、编排入口与安全边界（路径/文件管理）。前端直连 core 会丢失这些统一服务能力。
- 详细补充：补充“后端是文件与索引的可信边界”，把路径校验、异常语义和流式输出统一托管。
- 代码锚点：`backend/api.py:5`，`backend/api.py:341`，`backend/api.py:366`

### Q07：三种模式的工具权限是如何控制的？
- 标准回答：当前策略是三模式都允许同一套工具（`ALL_TOOLS`），差异主要在提示词和流程阶段控制，不在白名单差异。
- 详细补充：可以说明当前工具白名单一致是 MVP 取舍，未来若做风控可按模式细分可用工具。
- 代码锚点：`core/orchestration/policies.py:12`，`core/orchestration/policies.py:27`

### Q08：三种模式的分流逻辑是什么？
- 标准回答：先由 Router 产 Plan，再由 Runner 根据 `mode` 分发到 `run_learn_mode* / run_practice_mode* / run_exam_mode*`。这是单入口、多流程分支。
- 详细补充：建议加一句“先计划后执行”能让 learn/practice/exam 流程差异有明确代码落点。
- 代码锚点：`core/orchestration/runner.py:690`，`core/orchestration/runner.py:702`，`core/orchestration/runner.py:780`

### Q09：用户如何知道系统“正在工作”而不是卡死？
- 标准回答：流式工具链路会发 `__status__` 事件（如检索/工具执行状态），前端识别后用进度文案显示，最终继续输出正文。
- 详细补充：可强调状态事件不写入最终答案，只用于用户反馈，避免污染正文内容。
- 代码锚点：`core/llm/openai_compat.py:198`，`frontend/streamlit_app.py:717`

### Q10：引用显示为何不串历史？
- 标准回答：后端先发 `__citations__` 事件，前端按轮次缓存到 `_pending_citations`，流结束后 pop 到当前 assistant 消息并入历史；不会把旧轮引用混入新轮。
- 详细补充：关键点是“本轮引用本轮绑定”，通过 pending 缓存避免多轮引用串台。
- 代码锚点：`core/orchestration/runner.py:674`，`frontend/streamlit_app.py:699`，`frontend/streamlit_app.py:732`

### 整体项目追加拷打（15）

### Q76：为什么要把所有模式收敛到一个 Runner，而不是每个模式单独一套入口？
- 标准回答：统一入口能把“路由、检索、工具调用、记忆写回、流式事件”集中治理，避免三套流程长期漂移。这样上线后排障只需要先看 Runner 主链路，而不是在多个入口函数里来回跳。
- 详细补充：面试时可以强调这属于“中心编排”设计，牺牲了一些局部自由度，换来一致性和可维护性。
- 代码锚点：`core/orchestration/runner.py:30`，`core/orchestration/runner.py:690`，`core/orchestration/runner.py:780`

### Q77：为什么同时保留 `/chat` 和 `/chat/stream` 两个接口？
- 标准回答：`/chat` 适合同步场景（测试、脚本调用、结构化返回），`/chat/stream` 适合交互场景（降低等待焦虑、可展示状态进度）。两者共用同一编排内核，只是输出协议不同。
- 详细补充：这是“同一业务逻辑，多种交付形态”的接口设计，能兼顾稳定调用与产品体验。
- 代码锚点：`backend/api.py:334`，`backend/api.py:356`，`frontend/streamlit_app.py:378`

### Q78：Router 规划结果解析失败时，系统怎么保证不中断？
- 标准回答：Router 对模型输出做 JSON 解析，失败时直接回退默认 Plan（need_rag=true、按模式取 allowed_tools），让主链路继续执行，不把解析失败扩大为整次请求失败。
- 详细补充：这体现了“模型不可信、编排要兜底”的工程原则，避免把可恢复错误变成致命错误。
- 代码锚点：`core/agents/router.py:41`，`core/agents/router.py:53`，`core/agents/router.py:92`

### Q79：模式差异是靠提示词硬控，还是靠流程分支硬控？
- 标准回答：两者都有，但主导是流程分支硬控。Runner 在入口按 mode 进入 learn/practice/exam 不同函数，提示词是该分支内部的一层策略，不是唯一控制手段。
- 详细补充：面试回答时可以强调“提示词负责行为风格，流程分支负责业务语义”。
- 代码锚点：`core/orchestration/runner.py:705`，`core/orchestration/runner.py:709`，`core/orchestration/runner.py:793`

### Q80：工具调用环路如何防止模型无限调用工具？
- 标准回答：在 `openai_compat` 里设置了最大工具轮次上限 `max_rounds=6`，超过后强制收敛为最终回答，避免工具死循环拖垮时延和成本。
- 详细补充：这是典型“保护阈值”机制，防止极端 prompt 或模型异常导致不可控循环。
- 参数速记：`max_rounds=6`（同步/流式路径都有限制）。
- 代码锚点：`core/llm/openai_compat.py:75`，`core/llm/openai_compat.py:149`，`core/llm/openai_compat.py:212`

### Q81：当工具执行失败时，系统是直接报错还是有降级策略？
- 标准回答：工具层失败会记录日志并返回结构化错误；调用链路异常时 `openai_compat` 还有降级分支，回落到普通对话生成，保证请求尽量有可用输出。
- 详细补充：这属于“可用性优先”的落地策略，避免因单点工具故障导致整个会话失败。
- 代码锚点：`mcp_tools/client.py:898`，`core/llm/openai_compat.py:159`，`core/llm/openai_compat.py:290`

### Q82：为什么把工具能力放在 MCP 层，而不是塞进每个 Agent 类里？
- 标准回答：工具能力集中到 MCP 后，Agent 只关心“要不要调工具”和“如何使用结果”，工具协议、进程管理、错误语义在一处维护，减少重复逻辑。
- 详细补充：本质是把“业务决策（Agent）”和“能力执行（Tool Runtime）”解耦，便于后续扩展为远端 MCP。
- 代码锚点：`core/agents/tutor.py:94`，`mcp_tools/client.py:883`，`mcp_tools/server_stdio.py:109`

### Q83：参数配置是怎么治理的？如何避免“全靠改代码”？
- 标准回答：通用参数优先走 `.env`（如检索深度、切块、嵌入），业务强约束在代码里显式覆盖（如考试 `top_k=12`），形成“默认可配置 + 场景可覆盖”的分层。
- 详细补充：这种策略能兼顾可调试性和业务确定性，减少误配导致的线上行为漂移。
- 参数速记：代码默认 `TOP_K_RESULTS=3`，你当前 `.env` 配置为 `6`；考试路径显式覆盖为 `top_k=12`。
- 代码锚点：`rag/retrieve.py:104`，`core/orchestration/runner.py:275`，`rag/chunk.py:49`

### Q84：课程工作区是如何做隔离的？
- 标准回答：每个课程独立目录，按 `uploads/index/notes/mistakes/exams/practices` 分子目录管理，上传、索引、笔记都在课程命名空间内运行，避免跨课程污染。
- 详细补充：这是“文件系统级租户隔离”的简化实现，足够支撑单机多课程并行。
- 代码锚点：`backend/api.py:79`，`backend/api.py:108`，`backend/api.py:115`

### Q85：上传链路的安全底线有哪些？
- 标准回答：至少三层：课程存在校验、文件名净化（`basename` 防路径穿越）、扩展名白名单限制。这样可以防止非法路径写入和非支持格式进入解析链路。
- 详细补充：面试时可强调这属于输入边界防御，不是“可有可无”的前端校验。
- 代码锚点：`backend/api.py:163`，`backend/api.py:168`，`backend/api.py:170`

### Q86：为什么你们把状态、引用、正文拆成三类流事件？
- 标准回答：因为三类事件语义不同：状态用于反馈进度、引用用于证据展示、正文用于最终答案。混在一个文本流里会导致渲染污染和前端解析复杂度上升。
- 详细补充：这是“协议层分离语义”的做法，能显著降低前端状态机复杂度。
- 代码锚点：`core/llm/openai_compat.py:215`，`core/orchestration/runner.py:749`，`frontend/streamlit_app.py:712`

### Q87：前端“灰屏刷新”为什么会发生，架构上如何缓解？
- 标准回答：根因是 Streamlit 的 rerun 模型；每次交互会重跑脚本。当前通过 form 提交、`session_state` 状态持久化、`cache_data(ttl=30)` 缓存来减少无效刷新和重复请求。
- 详细补充：这是框架特性，不是单点 bug；优化目标是“降频”和“降抖动”，不是完全消灭 rerun。
- 参数速记：前端缓存 `ttl=30s`；请求超时分别为列表 `5s`、同步对话 `120s`、流式对话 `180s`、建索引 `300s`。
- 代码锚点：`frontend/streamlit_app.py:237`，`frontend/streamlit_app.py:469`，`frontend/streamlit_app.py:225`

### Q88：你们如何做链路可观测性，定位慢点在模型还是工具？
- 标准回答：日志记录了工具轮次、请求工具名、LLM 耗时和工具耗时，并带 `via=mcp_stdio` 链路字段。看到 `llm_ms` 高就查模型侧，`elapsed_ms` 高就查工具侧。
- 详细补充：这类结构化日志对线上定位很关键，比只打“成功/失败”有用得多。
- 代码锚点：`core/llm/openai_compat.py:100`，`core/llm/openai_compat.py:135`，`core/llm/openai_compat.py:272`

### Q89：如果要新增一个工具，最少要改哪些层？
- 标准回答：至少四层：`TOOL_SCHEMAS` 增 schema、`MCPTools._call_tool_local` 增实现分发、策略层把工具加入 allowed_tools、必要时在 prompt 里补工具使用规则。这样才会贯通“可被选中->可被执行->可被约束”。
- 详细补充：回答时可以展示“从协议到业务”的完整改动面，体现你对系统耦合点有全局认知。
- 代码锚点：`mcp_tools/client.py:21`，`mcp_tools/client.py:851`，`core/orchestration/policies.py:12`，`core/agents/tutor.py:94`

### Q90：从当前版本走向生产，你会优先补哪些工程能力？
- 标准回答：优先级通常是：鉴权与多用户隔离、持久化与备份策略、监控告警、限流与熔断、任务异步化（索引构建）、配置分环境管理。当前代码已具备基础分层，但生产治理能力还需要补齐。
- 详细补充：这题重点不是“功能清单”，而是你能否给出有顺序的工程落地路线。
- 代码锚点：`backend/api.py:38`，`backend/api.py:265`，`memory/manager.py:186`，`mcp_tools/client.py:511`

## RAG（解析/切块/索引/混合检索/引用）

### Q11：文档解析支持哪些格式？
- 标准回答：支持 PDF/TXT/MD/DOCX/PPTX/PPT。`.ppt` 先通过 COM 转 `.pptx` 再解析。解析失败按文件级容错，不阻断整批构建。
- 详细补充：可补充 `.ppt` 转换依赖 Windows COM，跨平台部署时通常建议统一为 `.pptx`。
- 代码锚点：`rag/ingest.py:170`，`rag/ingest.py:119`，`rag/ingest.py:153`

### Q12：切块策略是什么？
- 标准回答：按字符长度切块，支持 overlap；参数来自环境变量 `CHUNK_SIZE/CHUNK_OVERLAP`。有死循环防护：`overlap >= chunk_size` 时自动调整并加 next_start 兜底。
- 详细补充：建议说明 overlap 过大会重复信息、过小会丢上下文，当前是稳定优先的字符切块策略。
- 参数速记：环境变量默认值：`CHUNK_SIZE=512`、`CHUNK_OVERLAP=50`；你当前 `.env` 配置是 `600/120`。
- 代码锚点：`rag/chunk.py:13`，`rag/chunk.py:49`，`rag/chunk.py:22`，`rag/chunk.py:35`

### Q13：嵌入模型如何选设备和批量？
- 标准回答：`EMBEDDING_DEVICE=auto` 时优先 CUDA；模型名来自 `EMBEDDING_MODEL`；批量由 `EMBEDDING_BATCH_SIZE` 控制（GPU 默认更大）。查询向量按 BGE 规则可加前缀。
- 详细补充：可提到首轮会有模型加载开销，之后复用实例，吞吐主要受设备和 batch_size 影响。
- 参数速记：常用参数：`EMBEDDING_MODEL=BAAI/bge-base-zh-v1.5`、`EMBEDDING_DEVICE=auto`、`EMBEDDING_BATCH_SIZE=256`。
- 代码锚点：`rag/embed.py:31`，`rag/embed.py:55`，`rag/embed.py:62`，`rag/embed.py:21`

### Q14：向量索引如何构建与检索？
- 标准回答：使用 `FAISS IndexFlatL2`，入库时保存 chunks 元数据；检索返回距离并映射为 `score=1/(1+d)` 作为可读相关度分数。
- 详细补充：补一句当前是平面索引（IndexFlatL2），精度高但在超大库下查询成本线性增长。
- 代码锚点：`rag/store_faiss.py:27`，`rag/store_faiss.py:35`，`rag/store_faiss.py:44`

### Q15：BM25 是怎么实现的？
- 标准回答：项目内自实现 BM25（无外部依赖），中英文混合分词；预计算 TF/IDF，查询时按 BM25 公式打分并排序。
- 详细补充：可强调 BM25 在关键词命中上补足 dense 召回，特别是术语/缩写类查询。
- 代码锚点：`rag/lexical.py:15`，`rag/lexical.py:37`，`rag/lexical.py:56`，`rag/lexical.py:58`

### Q16：混合检索如何融合 dense + BM25？
- 标准回答：先分别召回候选，再用 RRF 融合（`HYBRID_RRF_K` + 双权重），最后截到 top_k；不是简单拼接。
- 详细补充：说明 RRF 的价值是“统一排序框架”，避免直接拼分数时不同量纲不可比。
- 参数速记：融合参数：`HYBRID_RRF_K=60`、`HYBRID_DENSE_WEIGHT=1.0`、`HYBRID_BM25_WEIGHT=1.0`（默认）。
- 代码锚点：`rag/retrieve.py:65`，`rag/retrieve.py:71`，`rag/retrieve.py:72`，`rag/retrieve.py:73`

### Q17：top-k 参数在三模式是否一致？
- 标准回答：默认 `Retriever.retrieve` 读 `TOP_K_RESULTS`；但考试模式在 Runner 显式传 `top_k=12`，会覆盖默认。
- 详细补充：回答时明确“默认值来自 .env，但业务分支可显式覆盖”，这是参数优先级问题。
- 参数速记：默认 `TOP_K_RESULTS` 读 `.env`（你当前是 `6`）；考试模式在 Runner 强制 `top_k=12`。
- 代码锚点：`rag/retrieve.py:104`，`core/orchestration/runner.py:275`，`core/orchestration/runner.py:325`

### Q18：引用是如何生成并展示的？
- 标准回答：Retriever 把 chunk 格式化为 `[来源N: doc_id, 页码]` 上下文；流式路径先发送 citations 事件，前端在消息下方渲染“查看引用来源”。
- 详细补充：补充引用来源来自 chunk 元数据（文档名/页码），不是模型自由生成。
- 代码锚点：`rag/retrieve.py:137`，`core/orchestration/runner.py:674`，`frontend/streamlit_app.py:647`

### Q19：索引重建流程如何保证一致性？
- 标准回答：`rebuild_indexes.py` 使用与后端相同的解析/切块/建库函数，保证离线重建与在线构建逻辑一致。
- 详细补充：可强调离线重建脚本复用同一管线，避免“线上检索逻辑”和“离线建库逻辑”漂移。
- 代码锚点：`rebuild_indexes.py:22`，`rebuild_indexes.py:56`，`rebuild_indexes.py:72`，`rebuild_indexes.py:76`

### Q20：当前 RAG 已知短板是什么？
- 标准回答：当前无 reranker、切块为字符级、BM25 中文粒度较粗；混合检索已改善但仍有排序上限，后续可加重排模型和语义切分。
- 详细补充：建议把短板分成“召回、排序、切块”三类讲，更像工程评估而不是泛泛而谈。
- 代码锚点：`rag/chunk.py:13`，`rag/lexical.py:15`，`rag/retrieve.py:65`

## MCP（协议子集/stdio链路/错误语义/重连/可观测性）

### Q21：工具调用主链路是什么？
- 标准回答：LLM 产生 tool_call 后，`openai_compat` 调 `MCPTools.call_tool`；该方法严格走 `_StdioMCPClient.call_tool`；server 端 `tools/call` 再落到 `_call_tool_local` 执行。
- 详细补充：可以按 4 跳链路回答：LLM tool_call -> MCP client -> stdio server -> 本地工具实现。
- 参数速记：工具循环上限 `max_rounds=6`，避免模型陷入工具死循环。
- 代码锚点：`core/llm/openai_compat.py:133`，`mcp_tools/client.py:883`，`mcp_tools/server_stdio.py:109`，`mcp_tools/client.py:851`

### Q22：实现了哪些 MCP 协议？
- 标准回答：当前仅 tools 标准子集：`initialize`、`notifications/initialized`、`tools/list`、`tools/call`。
- 详细补充：补一句这是“tools 子集实现”，当前不覆盖 resources/prompts/streamable transport。
- 参数速记：协议版本固定 `protocolVersion=2024-11-05`。
- 代码锚点：`mcp_tools/server_stdio.py:4`，`mcp_tools/server_stdio.py:96`，`mcp_tools/server_stdio.py:106`，`mcp_tools/server_stdio.py:109`

### Q23：工具 schema 如何从 OpenAI function 转 MCP？
- 标准回答：`_to_mcp_tools()` 从 `TOOL_SCHEMAS` 映射 `name/description/inputSchema`，避免双份 schema 分叉。
- 详细补充：强调 schema 单源化后，只需维护 TOOL_SCHEMAS 一处即可同时服务 LLM 和 MCP。
- 代码锚点：`mcp_tools/client.py:477`

### Q24：如何证明“严格仅 MCP，无本地 fallback”？
- 标准回答：`MCPTools.call_tool()` 失败时返回 `success:false, via:mcp_stdio`，不再本地直调；测试用 missing server 场景验证了这一点。
- 详细补充：建议指出失败时返回统一错误结构而非降级执行，保证链路语义一致可观测。
- 代码锚点：`mcp_tools/client.py:883`，`mcp_tools/client.py:900`，`tests/test_mcp_stdio.py:93`

### Q25：stdio MCP 客户端生命周期如何管理？
- 标准回答：按需拉起子进程，`_ensure_ready_locked` 中完成初始化握手；模块退出通过 `atexit` 清理。
- 详细补充：可补充懒启动减少空闲成本，atexit 清理避免遗留僵尸子进程。
- 参数速记：stdio client 请求超时 `request_timeout=20.0s`；进程异常会触发一次自动恢复流程。
- 代码锚点：`mcp_tools/client.py:281`，`mcp_tools/client.py:353`，`mcp_tools/client.py:359`，`mcp_tools/client.py:511`

### Q26：错误码语义如何对齐 JSON-RPC？
- 标准回答：方法不存在 `-32601`，参数错误 `-32602`，服务异常 `-32000`；参数类型不对也按 `-32602` 返回。
- 详细补充：回答时顺带说“参数错误和方法缺失分码返回”，方便客户端做精确提示。
- 代码锚点：`mcp_tools/server_stdio.py:87`，`mcp_tools/server_stdio.py:113`，`mcp_tools/server_stdio.py:131`，`mcp_tools/server_stdio.py:144`

### Q27：重连机制如何工作？
- 标准回答：`rpc/notify` 在 `_MCPTransportError` 时会重启并重试一次，降低瞬时故障影响；但有副作用工具重复执行风险（需幂等化）。
- 详细补充：重试是一把双刃剑：提升可用性，但对非幂等工具必须设计去重策略。
- 参数速记：`rpc/notify` 代码层重试上限为 `1` 次（循环 `range(2)`，首次失败后仅再试一次）。
- 代码锚点：`mcp_tools/client.py:381`，`mcp_tools/client.py:410`，`mcp_tools/client.py:420`

### Q28：为什么要把 print 重定向到 stderr？
- 标准回答：stdio 协议要求 stdout 仅输出帧消息；任何业务日志若写入 stdout 会污染 `Content-Length` 帧并导致协议解析失败。
- 详细补充：可加一句“stdout 污染会直接破坏帧边界”，这是 stdio 协议实现最常见坑。
- 代码锚点：`mcp_tools/server_stdio.py:26`，`mcp_tools/server_stdio.py:29`，`mcp_tools/server_stdio.py:38`

### Q29：链路可观测性是如何做的？
- 标准回答：工具执行日志记录 round/requested/executed/elapsed；工具结果默认补 `via=mcp_stdio` 字段，便于日志和前端定位调用路径。
- 详细补充：建议强调日志里有 round/requested/executed/elapsed，可直接定位慢点在 LLM 还是工具。
- 代码锚点：`core/llm/openai_compat.py:58`，`core/llm/openai_compat.py:183`，`mcp_tools/client.py:466`，`mcp_tools/client.py:894`

### Q30：MCP 这条链路目前有哪些自动化验证？
- 标准回答：有独立测试覆盖 server 可导入、初始化、tools/list、tools/call、错误码语义、严格无 fallback。
- 详细补充：可说明测试重点是协议连通性和错误语义，而不是工具业务正确性的全覆盖。
- 参数速记：当前核心覆盖 `4` 个协议方法（`initialize/notifications/initialized/tools/list/tools/call`）+ `2` 类关键错误码（`-32601/-32602`）。
- 代码锚点：`tests/test_mcp_stdio.py:15`，`tests/test_mcp_stdio.py:63`，`tests/test_mcp_stdio.py:80`，`tests/test_mcp_stdio.py:93`

## 记忆系统（情景记忆/用户画像/触发条件/检索注入）

### Q31：情景记忆分几种事件类型？
- 标准回答：`episodes.event_type` 设计为 `qa / mistake / practice / exam`。
- 详细补充：回答时把四类事件映射到业务场景：学习问答、练习错题、练习正常、考试记录。
- 参数速记：事件类型固定 `4` 类：`qa`、`mistake`、`practice`、`exam`。
- 代码锚点：`memory/store.py:41`

### Q32：学习模式问答什么时候写入记忆？
- 标准回答：当前在 `run_learn_mode_stream` 完成后写入 `qa`，并记录 `doc_ids` 到 metadata，同时通过统一入口 `record_event` 完成 `total_qa + 1`。
- 详细补充：补充学习模式写入时会带 doc_ids，后续检索能回到学习材料上下文。
- 参数速记：学习模式记忆写入参数：`event_type=qa`、`importance=0.5`、`increment_qa=True`，`doc_ids` 去重后写入 metadata。
- 代码锚点：`core/orchestration/runner.py:723`，`core/orchestration/runner.py:771`，`memory/manager.py:191`

### Q33：练习模式什么时候写入记忆？
- 标准回答：先通过 `_is_answer_submission` 判断用户是否在提交答案，进入评分后调用 `_save_grading_to_memory`，再统一走 `record_event` 写入。
- 详细补充：关键是先识别“是否作答”再评分入库，避免普通闲聊误写成练习记录。
- 参数速记：作答识别窗口看最近 `12` 条历史（`history[-12:]`）；命中后才进入评分写入链路。
- 代码锚点：`core/orchestration/runner.py:429`，`core/orchestration/runner.py:477`，`core/orchestration/runner.py:511`

### Q34：练习题“正确答案”会不会存入？
- 标准回答：会。评分后分数 >=60 写 `practice`（importance 0.4），<60 写 `mistake`（importance 0.9）。不是只存错题。
- 详细补充：可强调系统不是“只记错题”，正确样本也保存，用于刻画学习轨迹。
- 参数速记：判定阈值：分数 `>=60` 记 `practice`（`importance=0.4`），`<60` 记 `mistake`（`importance=0.9`）。
- 代码锚点：`backend/schemas.py:158`，`core/orchestration/runner.py:503`，`core/orchestration/runner.py:505`

### Q35：考试模式记忆现在是否已写入？
- 标准回答：已修复。考试批改命中后会调用 `_save_exam_to_memory`，并通过 `record_event` 写 `event_type=exam`，同步薄弱点与知识点掌握度到画像。
- 详细补充：建议补一句考试记录会同步画像薄弱点，形成“评估 -> 画像更新”的闭环。
- 参数速记：考试记忆常用参数：`weak_points` 截到前 `8` 个，`importance` 按分数分支（低分更高）。
- 代码锚点：`core/orchestration/runner.py:300`，`core/orchestration/runner.py:359`，`core/orchestration/runner.py:627`，`core/orchestration/runner.py:676`

### Q36：情景记忆存储结构是什么？
- 标准回答：SQLite `episodes` 表，字段含 `content`（自然语言摘要）、`importance`、`created_at`、`metadata(JSON)`，支持按课程和类型过滤索引。
- 详细补充：回答时突出 content 与 metadata 分离：前者便于检索展示，后者便于结构化扩展。
- 代码锚点：`memory/store.py:37`，`memory/store.py:45`，`memory/store.py:52`

### Q37：会把整段对话原文全量入库吗？
- 标准回答：不会。存的是事件摘要（如“问题+来源”“题目+学生答案+得分+标签”），metadata 存结构化附加信息（score/tags/doc_ids）。
- 详细补充：可补充这是隐私与成本取舍：不全量存原文可降低存储压力和提示词泄漏风险。
- 代码锚点：`memory/store.py:42`，`core/orchestration/runner.py:492`，`core/orchestration/runner.py:506`，`core/orchestration/runner.py:769`

### Q38：情景记忆如何检索？
- 标准回答：`search_episodes` 采用关键词 LIKE（分词 OR）、可选事件类型过滤、按 `importance DESC + created_at DESC` 排序，取 `top_k`。
- 详细补充：说明当前检索是关键词 LIKE 方案，优点是简单稳定，缺点是语义能力有限。
- 参数速记：记忆检索默认 `top_k=5`（`memory_search` 工具层），再按重要度和时间排序。
- 代码锚点：`memory/store.py:93`，`memory/store.py:111`，`memory/store.py:120`，`memory/store.py:130`

### Q39：什么情况下会触发检索？会不会把整条记录全塞模型？
- 标准回答：练习/考试路径会通过 `_fetch_history_ctx -> memory_search` 预取历史；注入时只取前 2 条、每条截断 120 字，不会把全库全量注入。
- 详细补充：建议强调检索结果进入模型前再次压缩，防止记忆上下文反客为主。
- 参数速记：注入模型前再压缩：只取前 `2` 条历史，每条最多 `120` 字；避免长上下文污染。
- 代码锚点：`core/orchestration/runner.py:142`，`core/orchestration/runner.py:400`，`core/orchestration/runner.py:406`，`core/orchestration/runner.py:419`

### Q40：用户画像存储结构是什么？
- 标准回答：`user_profiles` 以 `(user_id, course_name)` 为主键，字段含 `weak_points(JSON list)`、`concept_mastery(JSON dict)`、`pref_style`、`total_qa`、`total_practice`、`avg_score`。
- 详细补充：可补充画像是“课程级别聚合”，不是全局用户画像，隔离粒度更细。
- 参数速记：`concept_mastery` 结构为 `{知识点: {mastery(0~1), attempts, avg_score}}`；`pref_style` 当前默认值是 `step_by_step`。
- 代码锚点：`memory/store.py:55`，`memory/store.py:58`，`memory/store.py:205`

### Q41：用户画像如何更新？
- 标准回答：现在统一由 `record_event` 处理：写情景记忆后，同步更新 `total_qa/total_practice/avg_score`、`weak_points`，并按分数更新 `concept_mastery`。
- 详细补充：回答时点明更新策略是合并去重并截断，防止 weak_points 无限膨胀。
- 参数速记：画像 `weak_points` 上限 `20`；渲染展示时通常只展示前 `8`。
- 代码锚点：`memory/manager.py:136`，`memory/manager.py:191`，`memory/manager.py:239`，`core/orchestration/runner.py:511`

### Q42：用户画像在模型侧怎么使用？
- 标准回答：Router/Tutor/Grader 都会读取 `get_profile_context` 注入提示词；注入内容是摘要句，不是整行原始画像。
- 详细补充：可强调画像主要用于提示词增强，不直接参与评分逻辑，职责上与 grader 分离。
- 参数速记：注入时 `weak_points` 仅展示前 `8` 个；`concept_mastery` 仅展示“尝试次数 >=2 且掌握度最低”的前 `3` 个知识点。
- 代码锚点：`memory/manager.py:251`，`memory/manager.py:256`，`memory/manager.py:266`，`core/agents/tutor.py:76`

## 延伸追问（按上面四类补充）

### 7.1 整体项目（补充说明）
- 本轮按既定数量不新增整体项目延伸题，保持“整体项目 10 条”。

### 7.2 RAG 延伸追问（5）

### Q43：你怎么做 RAG 召回评测，指标怎么定？
- 标准回答：离线构造 query->relevant_chunks 标注集，按模式统计 Recall@K/Precision@K/HitRate@K；先对比 dense/bm25/hybrid，再看 top_k 和融合参数敏感性。
- 详细补充：建议先定义评测集（问题->标准证据），再分别看 Recall@k、MRR、最终答案命中率。
- 代码锚点：`rag/retrieve.py:104`，`rag/retrieve.py:108`，`rag/retrieve.py:113`
- 面试高频反问：如果 hybrid 没提升，你先排查哪三件事？

### Q44：BM25 中文单字分词会不会有噪声？
- 标准回答：会有噪声风险，尤其短 query。当前是轻量实现的工程折中，后续可升级词法分词或引入 query 重写。
- 详细补充：可补充单字切分对中文短查询有效，但噪声高时需结合停用词或最小词长策略。
- 代码锚点：`rag/lexical.py:15`，`rag/lexical.py:58`
- 面试高频反问：为什么没直接上外部分词器？

### Q45：混合检索参数有哪些可调？
- 标准回答：可调 `HYBRID_RRF_K`、dense/bm25 权重、两路候选倍数；这些参数共同决定“召回广度”和“排序偏好”。
- 详细补充：回答时建议给出调参顺序：先 RRF_K，再两路权重，最后再调候选池大小。
- 参数速记：默认值分别为 `HYBRID_RRF_K=60`、`HYBRID_DENSE_WEIGHT=1.0`、`HYBRID_BM25_WEIGHT=1.0`。
- 代码锚点：`rag/retrieve.py:71`，`rag/retrieve.py:72`，`rag/retrieve.py:73`，`rag/retrieve.py:117`
- 面试高频反问：你如何做参数寻优而不是拍脑袋？

### Q46：为什么考试模式 top_k 要强制 12？
- 标准回答：考试场景需覆盖更广的知识点和证据面，Runner 显式把检索深度拉高；这是业务策略覆盖默认参数的例子。
- 详细补充：可以解释为考试模式偏“覆盖率优先”，宁可上下文更长也要减少漏召回。
- 参数速记：考试模式固定 `top_k=12`，目的是扩大证据覆盖而不是追求最短上下文。
- 代码锚点：`core/orchestration/runner.py:275`，`core/orchestration/runner.py:325`
- 面试高频反问：top_k 过大带来的负面影响是什么？

### Q47：什么时候需要引入 reranker？
- 标准回答：当 top-k 里“有召回但排序不理想”频繁出现时，reranker能显著提升前几位精度。当前链路未引入 reranker，属于后续提升空间。
- 详细补充：当你发现“相关块已召回但排序靠后”时，就是引入 reranker 的典型信号。
- 代码锚点：`rag/retrieve.py:65`，`rag/retrieve.py:137`
- 面试高频反问：你会把 reranker 放在 dense 前还是后？

### 7.3 MCP 延伸追问（5）

### Q48：你们 RPC 的并发模型是什么，乱序响应会怎样？
- 标准回答：当前客户端是串行锁模型，单请求 in-flight，`id` 匹配是兜底。若未来并发化，需要引入 pending-map 管理多请求响应匹配。
- 详细补充：当前串行 in-flight 简化并发问题，但吞吐受限；并发化要加 pending-id 路由。
- 代码锚点：`mcp_tools/client.py:394`，`mcp_tools/client.py:410`
- 面试高频反问：并发化后你如何保证线程安全？

### Q49：`protocolVersion` 变更时如何兼容？
- 标准回答：当前是严格版本（客户端/服务端都用 `2024-11-05`），不兼容即初始化失败；后续可做“能力协商+向后兼容矩阵”。
- 详细补充：补一句版本不一致在 initialize 阶段暴露，比运行中失败更易定位。
- 参数速记：客户端与服务端都声明 `2024-11-05`；版本不一致在初始化阶段直接失败。
- 代码锚点：`mcp_tools/client.py:179`，`mcp_tools/client.py:366`，`mcp_tools/server_stdio.py:100`
- 面试高频反问：如何设计兼容测试用例？

### Q50：为什么 `_call_tool_local` 不算 fallback？
- 标准回答：它位于 server 端，承接 `tools/call` 的执行层；应用侧已不再本地直调。路径语义是“远程（子进程）执行”，不是“失败回退本地执行”。
- 详细补充：强调 `_call_tool_local` 是 server 执行层，不是 client 失败后的绕过路径。
- 代码锚点：`mcp_tools/server_stdio.py:117`，`mcp_tools/client.py:883`，`mcp_tools/client.py:851`
- 面试高频反问：如果将来有远程 MCP server，这层怎么抽象？

### Q51：`tools/call` 重试会不会造成副作用重复？
- 标准回答：会有风险，尤其 filewriter append 场景。当前通过“重试一次”提升可用性，但幂等保障需要工具层补 request-id 去重。
- 详细补充：建议举 filewriter 例子：重试可能追加两次，因此必须引入幂等键。
- 参数速记：`tools/call` 传输故障最多自动重试 `1` 次；有副作用工具需做幂等防重。
- 代码锚点：`mcp_tools/client.py:410`，`mcp_tools/client.py:420`，`mcp_tools/client.py:430`
- 面试高频反问：你会在 client 还是 server 做幂等控制？

### Q52：为什么要显式打 `via=mcp_stdio`？
- 标准回答：这是链路审计字段，能快速证明调用经过 MCP；日志聚合后可用于统计工具成功率和故障定位。
- 详细补充：可补充 `via` 字段能在日志聚合中快速做“链路占比”和“故障分层”统计。
- 代码锚点：`mcp_tools/client.py:466`，`mcp_tools/client.py:894`，`tests/test_mcp_stdio.py:88`
- 面试高频反问：除了 via，你还会补哪些 trace 字段？

### 7.4 记忆系统延伸追问（5）

### Q53：`memory_search` 返回结构为什么要从字符串改成对象？
- 标准回答：对象结构可同时保留 `content/summary/metadata`，避免下游依赖字符串解析；并兼容前后端不同展示需求。
- 详细补充：对象化返回让前端展示和下游拼装更稳定，减少“字符串协议”带来的兼容负担。
- 代码锚点：`mcp_tools/client.py:823`，`mcp_tools/client.py:829`，`core/orchestration/runner.py:408`
- 面试高频反问：如何保证旧调用方不崩？

### Q54：`weak_points` 为什么最多保留 20？
- 标准回答：避免画像无限膨胀导致提示词污染；同时保持“最近错误优先”的学习引导策略。
- 详细补充：上限控制的本质是 prompt 预算管理，避免用户画像反向拉高上下文成本。
- 参数速记：`weak_points` 保留上限是 `20`，超过后按“新优先”截断。
- 代码锚点：`memory/manager.py:136`，`memory/manager.py:285`
- 面试高频反问：20 这个阈值如何数据驱动优化？

### Q55：importance 目前是固定打分吗？会衰减吗？
- 标准回答：当前是规则赋值（错题高、普通低），未做时间衰减。排序用 `importance + created_at`，是可解释但偏静态的策略。
- 详细补充：可以坦诚当前是规则分，不是学习型权重；优点可解释，缺点适应性一般。
- 参数速记：当前规则分值：练习错题常用 `0.9`，练习正确 `0.4`，学习问答约 `0.5`，考试按分数分支 `0.9/0.6`。
- 代码锚点：`core/orchestration/runner.py:505`，`core/orchestration/runner.py:674`，`memory/store.py:130`
- 面试高频反问：你会怎么设计可学习的 importance？

### Q56：学习模式非流式为什么不写 `qa` 记忆？
- 标准回答：当前仅流式路径实现了写入，这是实现范围选择，不是模型限制。若要统一语义，需在非流式 learn 结束后补同样写入逻辑。
- 详细补充：回答时可直接说这是实现覆盖差异，后续可在非流式 learn 完成后补同样写入。
- 代码锚点：`core/orchestration/runner.py:65`，`core/orchestration/runner.py:721`
- 面试高频反问：补齐后如何避免重复写入？

### Q57：多用户隔离如何保证？
- 标准回答：存储层按 `user_id + course_name` 设计，但当前 manager 默认 `user_id="default"` 单例复用，真实多用户需在请求入口透传 user_id。
- 详细补充：关键风险是默认 user_id 会把多用户数据混在一起，生产必须从入口透传。
- 参数速记：当前默认 `user_id="default"` 且全局 `_default_manager` 为单例 `1` 份；多用户场景必须在入口显式透传 user_id。
- 代码锚点：`memory/store.py:64`，`memory/manager.py:331`
- 面试高频反问：如果要支持租户隔离，你会改哪一层？

### 7.5 前后端基础拷打（18）

### Q58：这个项目里前端和后端分别负责什么？
- 标准回答：前端（Streamlit）是“交互层”，负责用户操作和可视化体验，包括课程选择、模式切换、文件上传、流式回答展示、引用展示和导图展示。后端（FastAPI）是“服务层”，负责状态管理与业务执行，包括工作区生命周期、文档解析、索引构建、编排入口、SSE 推流和错误语义统一。简单说，前端负责“看见什么、怎么点”，后端负责“事情怎么做、怎么稳定做”。
- 详细补充：建议一句话区分：前端负责交互体验，后端负责稳定执行与数据生命周期。
- 代码锚点：`frontend/streamlit_app.py:456`，`frontend/streamlit_app.py:683`，`backend/api.py:265`，`backend/api.py:335`

### Q59：能不能不要后端，让前端直接调 core？
- 标准回答：理论上可以做本地 Demo，但工程上不建议。因为上传文件、构建索引、流式协议输出、统一异常处理、本地磁盘操作这些都属于服务职责，放到前端脚本会导致 UI 进程过重、耦合高、调试和扩展都变差。保留后端的核心价值是把“可复用的业务能力”从“页面渲染逻辑”中解耦出来。
- 详细补充：可补充“前端直连 core”常见问题是权限边界缺失、异常语义不统一、复用性差。
- 代码锚点：`backend/api.py:150`，`backend/api.py:265`，`backend/api.py:355`

### Q60：FastAPI 在这个项目里起什么作用？
- 标准回答：FastAPI 提供了这个项目的标准服务壳：HTTP 路由分发、请求/响应模型校验、状态码和异常语义、流式响应封装。它把 core 编排能力包装成稳定 API，对前端来说就是统一的调用入口。这样做的好处是后续换前端或接入其他客户端时，后端接口层基本可以复用。
- 详细补充：回答时强调 FastAPI 给了类型化接口和统一异常模型，适合作为编排系统外壳。
- 代码锚点：`backend/api.py:13`，`backend/api.py:34`，`backend/api.py:98`

### Q61：Streamlit 在这个项目里起什么作用？
- 标准回答：Streamlit 是快速交互前端框架，适合把 AI 工作流快速产品化。这里它承载了课程与模式操作、聊天输入输出、流式文本渲染、引用面板、导图展示等 UI 能力。它的优势是开发快、状态管理简单；代价是 rerun 模型带来的刷新感，需要额外做缓存和状态细分优化。
- 详细补充：可补充 Streamlit 适合内部工具和教学场景，若追求复杂交互再考虑前后端分离框架。
- 代码锚点：`frontend/streamlit_app.py:218`，`frontend/streamlit_app.py:643`，`frontend/streamlit_app.py:727`

### Q62：后端核心接口有哪些？
- 标准回答：核心接口可以分为五组：工作区 CRUD、文件上传与列表、索引构建与删除、同步对话 `/chat`、流式对话 `/chat/stream`。其中 `/chat` 偏“请求-响应”，`/chat/stream` 偏“边生成边返回”的交互体验。接口分层清晰后，前端调用可以按“管理类操作”和“对话类操作”分开处理。
- 详细补充：建议按“管理接口 vs 对话接口”分组回答，结构更清晰也更像工程设计。
- 代码锚点：`backend/api.py:107`，`backend/api.py:150`，`backend/api.py:265`，`backend/api.py:334`，`backend/api.py:355`

### Q63：请求参数是怎么做结构化校验的？
- 标准回答：主要通过 Pydantic 模型定义契约，然后由 FastAPI 在入口自动校验。比如 `ChatRequest` 限定 `course_name/mode/message/history` 结构，`ChatResponse` 约束返回格式。好处是接口边界清晰、错误更早暴露、前后端联调时更容易定位字段问题。
- 详细补充：可强调 schema 校验把错误前置到接口层，避免坏数据进入核心编排流程。
- 代码锚点：`backend/schemas.py:14`，`backend/schemas.py:78`，`backend/schemas.py:86`，`backend/api.py:334`

### Q64：为什么要配 CORS？
- 标准回答：CORS 是浏览器安全策略下的跨域放行机制。开发阶段前后端端口不同、部署阶段域名不同都可能触发跨域限制，所以后端要显式声明允许策略。即使本机可跑，提前配置 CORS 可以减少环境切换时的“线上能跑/本地报跨域”问题。
- 详细补充：补一句当前即使同机开发也建议保留 CORS 配置，减少部署切换风险。
- 代码锚点：`backend/api.py:15`，`backend/api.py:37`

### Q65：流式输出（SSE）是怎么实现的？
- 标准回答：后端在 `chat_stream` 中迭代 Runner 的输出，把每个 chunk 包装成 SSE `data:` 行并持续推送；前端建立流式请求后逐行消费，再交给 `st.write_stream` 渲染。这样用户不用等完整答案，可以先看到首段输出与进度状态。工程上等价于把一次大响应拆成连续小响应，显著改善体感延迟。
- 详细补充：回答时点明 SSE 优化的是首字节体验（TTFB 感知），不是总生成时延本身。
- 参数速记：前端流式请求超时参数是 `timeout=180s`；后端持续发送 SSE chunk。
- 代码锚点：`backend/api.py:355`，`backend/api.py:364`，`backend/api.py:383`，`frontend/streamlit_app.py:377`，`frontend/streamlit_app.py:727`

### Q66：前端怎么处理“状态事件”和“正文事件”？
- 标准回答：前端把流事件分成三类：正文文本、状态事件、引用事件。状态事件（`__status__`）只用于提示“正在检索/调用工具”，不进入最终回答；引用事件（`__citations__`）先缓存，等本轮结束再绑定到当前 assistant 消息。这个分流设计避免了“字典串进正文”或“引用串台”的常见问题。
- 详细补充：可强调状态/引用/正文三路分流是为了解决“可观测性”和“输出纯净性”的冲突。
- 代码锚点：`frontend/streamlit_app.py:712`，`frontend/streamlit_app.py:717`，`frontend/streamlit_app.py:732`

### Q67：`st.session_state` 在这里解决了什么问题？
- 标准回答：Streamlit 的执行模型是每次交互都重跑脚本，所以如果不用 `session_state`，聊天历史和页面状态会频繁丢失。当前把课程、模式、历史消息、临时引用等都放进 `session_state`，确保 rerun 后还能恢复上下文。它本质上是这个前端的“会话内状态容器”。
- 详细补充：建议说明 session_state 是 Streamlit 下维持会话一致性的核心机制。
- 代码锚点：`frontend/streamlit_app.py:225`，`frontend/streamlit_app.py:229`，`frontend/streamlit_app.py:777`

### Q68：`@st.cache_data` 的作用是什么？
- 标准回答：`@st.cache_data` 用于缓存读多写少的数据，降低重复请求开销和页面抖动。这里主要缓存课程列表和文件状态，避免每次 rerun 都发网络请求。写操作后手动 `clear()`，能兼顾“性能”与“数据新鲜度”。
- 详细补充：可以补充缓存要配合失效策略，否则会出现“数据新鲜度不足”的副作用。
- 参数速记：缓存参数：`@st.cache_data(ttl=30)`，写操作后会手动 `clear()`。
- 代码锚点：`frontend/streamlit_app.py:237`，`frontend/streamlit_app.py:249`，`frontend/streamlit_app.py:261`

### Q69：上传文件这条链路怎么走？
- 标准回答：链路是“前端选择文件 -> POST 上传 -> 后端校验并落盘 -> 更新工作区状态”。后端会做三层保护：课程存在校验、文件名安全校验（防路径穿越）、扩展名白名单校验。通过后写入课程 `uploads` 目录，再同步到工作区文档记录。
- 详细补充：重点说三层安全校验（课程存在、文件名安全、扩展名白名单）是上传链路底线。
- 代码锚点：`frontend/streamlit_app.py:523`，`frontend/streamlit_app.py:303`，`backend/api.py:150`，`backend/api.py:165`，`backend/api.py:171`

### Q70：构建索引是前端做还是后端做？
- 标准回答：索引构建完全在后端执行，前端只负责触发和展示结果。后端流水线是“解析 -> 切块 -> 向量化 -> 建 FAISS -> 保存”，这是 CPU/GPU 和 I/O 都较重的任务，放后端更符合职责分层。前端只需感知进度与结果，不参与重计算。
- 详细补充：可补充构建索引属于重任务，放后端便于控制资源和做超时/重试策略。
- 参数速记：构建索引调用前端等待超时 `timeout=300s`（首次下载模型时更稳妥）。
- 代码锚点：`frontend/streamlit_app.py:317`，`backend/api.py:265`，`backend/api.py:302`，`backend/api.py:312`

### Q71：出错时前后端如何处理？
- 标准回答：后端在异常点统一抛 `HTTPException`，返回明确状态码和 `detail`，保证客户端可判读。前端收到非 200 响应会优先显示后端 detail；对超时、网络异常等也有本地兜底提示。这样可以把“业务错误”和“传输错误”区分开，便于定位。
- 详细补充：回答时建议区分“业务异常（4xx/5xx）”和“网络异常（超时/断连）”两类。
- 参数速记：同步聊天接口前端请求超时约 `timeout=120s`，并区分业务错误与网络错误。
- 代码锚点：`backend/api.py:111`，`backend/api.py:329`，`frontend/streamlit_app.py:292`，`frontend/streamlit_app.py:330`

### Q72：模式切换后为什么行为会变化？
- 标准回答：模式切换不是只换 UI 文案，而是换后端执行流程。前端把 mode 传给后端后，Runner 会进入 learn/practice/exam 的不同分支，并绑定不同提示词、检索深度和后处理逻辑。所以模式变化本质是“编排策略变化”，不是“前端样式变化”。
- 详细补充：可强调 mode 不是 UI 标签，而是直接驱动不同编排分支和后处理策略。
- 代码锚点：`frontend/streamlit_app.py:504`，`frontend/streamlit_app.py:515`，`core/orchestration/runner.py:702`，`core/orchestration/runner.py:721`

### Q73：聊天历史是如何传递到后端的？
- 标准回答：前端先裁剪最近若干轮历史，再构造成 `role/content` 的轻量 payload 发送给后端。后端把 `ChatRequest.history` 转成 dict 列表后传给 Runner，再由 Tutor 按 `history_limit` 注入模型消息。这个设计在前端和后端各做一层控制，避免历史无限膨胀。
- 详细补充：补充历史先以轻量结构跨层传递，再在 agent 层决定最终注入粒度。
- 参数速记：历史链路关键值：前端裁剪 `20`，Tutor 默认 `history_limit=20`，考试常传 `30`。
- 代码锚点：`frontend/streamlit_app.py:343`，`frontend/streamlit_app.py:381`，`backend/api.py:346`

### Q74：为什么这个前端“经常刷新”，有没有做优化？
- 标准回答：根因是 Streamlit 的 rerun 机制：按钮、输入、状态变化都会触发整页脚本重跑。当前已做三类优化：用 `form` 降低无效重跑、用缓存减少重复请求、用事件分流减少流式期间的 UI 抖动。它不能完全消除刷新，但可以明显降低“灰屏感”和卡顿感。
- 详细补充：建议指出刷新不可完全消除，只能通过 form、缓存和状态拆分降低体感。
- 代码锚点：`frontend/streamlit_app.py:467`，`frontend/streamlit_app.py:237`，`frontend/streamlit_app.py:699`

### Q75：后端怎么启动，开发态有什么特点？
- 标准回答：后端入口是模块方式启动 `python -m backend.api`，内部由 Uvicorn 托管 ASGI 应用。当前配置 `reload=True`，开发态修改代码会自动重启服务，联调效率高。上线时通常关闭 reload，并交给进程管理器统一托管。
- 详细补充：可补充 reload 仅适合开发环境，生产应关闭并由进程管理器托管。
- 参数速记：后端默认端口 `API_PORT=8000`；开发态 `reload=True(1)`，生产建议 `reload=False(0)`。
- 代码锚点：`backend/api.py:393`，`backend/api.py:399`
