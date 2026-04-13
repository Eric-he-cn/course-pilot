# CoursePilot 面试准备（2026-04-13）

> 这份材料按“能直接讲出口”的思路整理，基于当前仓库实现、文档和评测产物，不写历史理想态。

---

## 1. 项目一句话介绍

CoursePilot 是一个面向大学课程学习场景的 AI 学习助手，核心不是做通用聊天，而是把“教材检索 + 学习讲解 + 练习/考试 + 评分讲评 + 学习记忆 + 动态评测”做成一个可验证、可持续演进的工程系统。

---

## 2. 30 秒版本自我介绍

如果面试官让你快速介绍项目，可以这样说：

> 我做的是一个课程学习 Agent 系统，叫 CoursePilot。它围绕大学课程场景，把教材上传、RAG 检索、学习讲解、练习出题、考试生成、自动评分、错题记忆和评测闭环整合到一个系统里。  
> 我们在 v3 里重点做了三件事：第一，把自由度很高的 Agent 流程收敛成模板化工作流；第二，把练习和考试改成 artifact-first，解决评分链路不稳定的问题；第三，把 bench、judge、review 做成动态评测闭环，让系统优化有客观依据。

---

## 3. 2 分钟版本项目叙事

### 3.1 业务目标

- 目标用户是课程学习场景，而不是开放域闲聊。
- 目标能力是“讲得明白、题出得准、评分稳定、能记住薄弱点、能持续评估迭代”。

### 3.2 系统主链路

- 前端用 Streamlit，后端用 FastAPI。
- 用户通过 `/chat` 或 `/chat/stream` 进入编排层。
- Router 先把自然语言请求归一化成 `workflow_template + action_kind`。
- Runtime 把模板编译成白名单 `TaskGraph`。
- 运行时并行预取 RAG 和 Memory，上下文最后由具体 Agent 选择组织。
- Tutor 负责讲解，QuizMaster 负责出题/出卷，Grader 负责评分。
- ToolHub 统一处理工具权限、预算、去重、幂等和审计。
- 最后把 SessionState、练习记录、考试记录、记忆信息持久化。

### 3.3 为什么这样设计

- 因为课程学习系统不是“能答就行”，还要追求稳定性、可恢复性和可评估性。
- 所以 v3 的核心设计不是更自由，而是更可控。

---

## 4. 你最该讲清楚的 4 个技术亮点

### 4.1 模板化工作流，而不是自由多 Agent 乱跑

核心点：

- Router 只能从 6 个固定模板里选：
  - `learn_only`
  - `practice_only`
  - `exam_only`
  - `learn_then_practice`
  - `practice_then_review`
  - `exam_then_review`
- Runtime 再把模板编译成 `TaskGraph`，只执行白名单步骤。

你可以这样讲：

> 我们没有追求完全自治式多 Agent，因为课程学习场景更看重稳定性和链路可控。v3 里我把流程收敛为模板化工作流，再编译成白名单 TaskGraph，这样每一轮执行路径都是可解释、可审计、可回放的。

### 4.2 SessionState 和 AgentContext 分层

核心点：

- `SessionState` 是跨轮、跨 Agent 的短期状态真源。
- `AgentContext` 是单个 Agent 生命周期内的上下文快照。
- 这样可以避免“谁都改 history，最后状态不一致”的问题。

你可以这样讲：

> 我们把全局短期状态和单 Agent 上下文做了分层。SessionState 负责跨轮恢复，比如当前阶段、活跃练习、最近评分结果；AgentContext 只负责这一轮当前 Agent 真正需要的上下文组织。这个设计让恢复、持久化和调试都清晰很多。

### 4.3 Artifact-first，保证练习/考试评分稳定

核心点：

- QuizMaster 先产出结构化 `PracticeArtifactV1 / ExamArtifactV1`。
- 只有 validator 通过，题面才会展示。
- Grader 只吃 artifact + submission，不再从历史消息反推题目。

你可以这样讲：

> 这是我觉得项目里最重要的稳定性设计之一。以前评分很容易受历史消息污染，或者坏 JSON、空试卷导致链路失稳。artifact-first 之后，生成和评分之间的契约明确了，评分不再依赖 history 猜题，大幅降低了不确定性。

### 4.4 工具治理不是“能调就调”，而是统一进 ToolHub

核心点：

- 统一权限级别：`safe / standard / elevated`
- 统一预算：`per_request_total / per_round / per_tool`
- 去重、幂等、审计全部在 ToolHub 里做

你可以这样讲：

> 很多 Agent 项目只关注工具可调用，但生产可用还要看治理。我这边把工具收口到 ToolHub，统一做权限、预算、去重和审计，这样能限制工具滥用，也更容易回溯问题。

---

## 5. 面试官很可能追问的 8 个问题

### 5.1 为什么不用完全自治 Agent？

答法：

> 因为课程学习属于强约束场景。用户会关心是否引用教材、是否按阶段出题、评分是否稳定、状态能否恢复。完全自治虽然灵活，但更容易出现路径漂移、重复调用工具、状态不一致。我们最后选择“Router 规划 + Runtime 白名单执行”，本质是用工程约束换系统稳定性。

### 5.2 Router 和 Runtime 分别负责什么？

答法：

> Router 负责“理解用户意图并选模板”，Runtime 负责“把模板编译成 TaskGraph 并执行”。Router 更像语义规划器，Runtime 更像治理层和执行器。

### 5.3 你怎么做 RAG，而不是只说“用了向量检索”？

答法：

> 我们支持课程级文档入库、切块、FAISS 索引和检索服务，按模式区分 top-k，学习/练习默认 4，考试默认 8。系统强调引用可追溯，而且评测里会单独看 `hit_at_k / top1_acc / precision_at_k / gold_case_coverage`，不是只看主观回答好不好。

### 5.4 你怎么做动态评测？

答法：

> 我们做了 `bench -> judge -> review` 三段式闭环。bench 负责跑基准样本和采集 trace，judge 用独立模型打分，review 再综合延迟、回退、检索命中、judge 结果生成回归队列和人工复审队列。这样每次改系统，不是拍脑袋说“感觉变好了”，而是有可追踪指标。

### 5.5 最近解决过什么真实问题？

答法：

> 一个典型问题是 full30 的 RAG 指标曾经全 0，但人工看引用其实不是全错。最后排查到是评测口径过于依赖 `case_id -> gold_doc_ids` 单一路径，一旦 case_id 对不上就会静默产出全 0。后面我补了 gold 覆盖率校验、多策略命中判定和离线重算模式，把这个误导性指标修正过来。

### 5.6 你怎么处理评分链路的不稳定？

答法：

> 一方面通过 artifact-first 保证输入契约稳定；另一方面 practice/exam 的评分都由 Grader 消费结构化 artifact，不再从 history 逆推题目，这样错误更容易收敛在单点。

### 5.7 这个系统目前最大的不足是什么？

答法：

> 当前最大的短板不是检索命中，而是练习和考试场景的回答质量与时延。比如 judge 结果里 learn 模式明显更好，practice 是目前最弱的一段；review 结果也显示还有回归队列，主要集中在低分 case 和延迟回归。

### 5.8 下一步你会怎么优化？

答法：

> 我会优先做两类优化：第一，针对 practice/exam 做更强的 artifact 约束和 rubric 细化；第二，针对 latency 做链路压缩，比如减少不必要的 LLM 调用、优化上下文预算和流式首 token 时间。

---

## 6. 可以直接背的数据点

以下数字来自当前仓库产物，面试时够用了：

- `final_full30` 检索指标：
  - `hit_at_k = 1.0`
  - `top1_acc = 1.0`
  - `precision_at_k = 1.0`
  - `gold_case_coverage = 1.0`
- `round2_full30_judge`：
  - `num_judged = 30`
  - `avg_overall_score = 0.7013`
- 分模式 judge：
  - `learn = 0.849`
  - `practice = 0.556`
  - `exam = 0.699`
- `round2_full30_review`：
  - `regression_case_count = 11`
  - `human_review_queue_count = 20`
  - `fallback_case_count = 0`
  - `trace_contract_error_count = 0`

建议说法：

> 当前最强的是检索证据层，full30 的命中指标和 gold 覆盖率已经做到 1.0；但生成质量还没到头，judge 平均分在 0.70 左右，其中 learn 最强，practice 最需要继续打磨。

---

## 7. 3 个最好用的 STAR 故事

### 7.1 故事一：把自由编排收敛成模板化 Runtime

S：
系统从“旧编排方式”往 v3 迁移，需要兼顾稳定性和兼容性。

T：
既要提升流程可控性，又不能一刀切重写导致系统不可用。

A：

- 设计 `workflow_template + action_kind`
- 增加 `ExecutionRuntime + TaskGraph`
- 保留兼容入口 `OrchestrationRunner`
- 让 SessionState 成为短期状态真源

R：

- 执行路径可审计
- taskgraph 状态可落盘
- fallback 和 trace contract 更容易观测

### 7.2 故事二：修复 RAG 指标全 0 的假回归

S：
full30 检索指标出现全 0，但引用内容并不符合这个结论。

T：
判断到底是系统真实退化，还是评测口径有问题。

A：

- 复核 raw 样本与 gold 对齐方式
- 增加 `gold_case_coverage` 防呆
- 增加 chunk/doc+page/doc/page/keyword 多策略匹配
- 增加 `--recompute-only` 离线重算

R：

- 检索指标恢复到可解释状态
- 避免错误结论影响架构判断

### 7.3 故事三：修复 baseline judge 缺失语义问题

S：
v2 baseline 没有 judge，但 review 把 baseline judge 缺失算成 0 分。

T：
避免评测报告误导决策。

A：

- 给 review 增加 judge 有效性判定
- baseline 无 judge 时输出 `null / N/A`
- 同时修复 judge 脚本不自动加载 `.env` 的问题

R：

- 评测语义更准确
- 报告能区分“没有数据”和“分数很低”

---

## 8. 面试时容易说错的点

- 不要说这是“完全自动的多 Agent 系统”。
  - 更准确的说法是“模板化多 Agent 编排系统”。
- 不要说“评分完全靠 history 理解题目”。
  - 现在是 artifact-first。
- 不要把评测说成“只看 LLM judge”。
  - 实际上还有 bench 指标、trace、review 队列。
- 不要把当前系统包装成“已经完全优化完成”。
  - 更真实的说法是：检索和执行治理比较成熟，但 practice/exam 质量和延迟仍有优化空间。

---

## 9. 最稳的一版回答模板

> 这个项目里我主要关注的是把 AI 学习助手做成一个工程系统，而不是只做一个能聊天的 demo。  
> 技术上我做了三层收敛：第一层是流程收敛，把用户请求统一路由到固定 workflow template，再编译成白名单 TaskGraph；第二层是数据收敛，用 SessionState 管理短期状态，用 artifact-first 保证出题和评分链路稳定；第三层是验证收敛，把 bench、judge、review 做成闭环，让优化有客观证据。  
> 当前项目的检索证据层已经比较稳定，full30 的命中和 gold 覆盖都做到 1.0，但生成质量还有提升空间，特别是 practice 场景。我最近一轮工作的重点，就是修复评测口径问题、补强 judge/review 语义，并把整个 v3 收官文档整理清楚。

---

## 10. 面试前 10 分钟速记词

- 课程学习闭环
- 模板化工作流
- Runtime 白名单执行
- TaskGraph
- SessionState 真源
- AgentContext 分层
- artifact-first
- ToolHub 治理
- bench / judge / review
- 可解释、可恢复、可评测

---

## 11. 使用建议

如果你时间很紧，建议只背下面四段：

1. 一句话项目介绍
2. 4 个技术亮点
3. 3 个 STAR 故事里的任意 2 个
4. 那组关键数据点

如果面试官偏架构，就重点讲 Runtime、SessionState、artifact-first。  
如果面试官偏工程能力，就重点讲评测闭环、RAG 指标修复、judge/review 语义修复。  
如果面试官偏产品理解，就强调这是“课程学习闭环”而不是泛聊天机器人。

