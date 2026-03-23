可以把 **context engineering** 理解成：

**不是只写 prompt，而是系统性设计“模型这一轮到底能看到什么、以什么顺序看到、看到多少、哪些不该看到”**。
它关心的是 **上下文的选择、压缩、结构化、时机控制、权限边界、以及跨轮持久化**。Anthropic 在 2025 年把它明确概括为：随着 agent 系统变复杂，关键不再只是 prompt wording，而是对有限上下文进行“策展式管理”；LangChain/LangGraph 也把短期记忆、长期记忆、线程状态、持久化 store 分开建模；OpenAI 的 agent/context 文档则把 trimming、compression、tool-first retrieval 作为核心模式。([Anthropic][1])

---

## 一、什么是 context engineering

如果说 prompt engineering 主要在解决：

* 你要模型“怎么做”

那么 context engineering 解决的是：

* 你要模型“基于哪些信息来做”
* 这些信息“怎么组织”
* 哪些信息应该常驻，哪些按需拉取
* 多轮对话变长后怎么裁剪
* tool / RAG / memory 返回的结果如何压缩成对模型最有用的形式
* 多 agent 之间如何避免彼此污染上下文

所以它本质上是一个 **LLM 输入编排系统**。

一个更实用的定义是：

> **Context engineering = 对 system prompt、用户输入、会话历史、摘要、RAG 结果、长期记忆、工具结果、计划状态、agent 中间产物进行动态编排的工程。**

这也是近两年业内主流 agent 系统的共同趋势：
**少做一次性“把所有东西塞进 prompt”，多做“按需检索 + 分层记忆 + 中间状态结构化 + 压缩注入”**。Anthropic 明确强调 agent 场景不应在启动时把所有信息都预先塞给模型，而应让模型借助工具逐步获取所需上下文；LangChain 也强调你必须精确控制“每一步传给模型什么”。([Anthropic][1])

---

## 二、为什么 RAG + 长期记忆 + multi-agent 系统更需要它

因为这类系统天然有三个问题：

### 1）信息源过多

你至少会有：

* system prompt
* 当前用户问题
* 历史对话
* 对话摘要
* 用户画像
* 历史重要事件
* RAG 检索片段
* 工具调用结果
* planner / router / specialist agent 的中间结论

如果不设计，最后就会变成：
**都想放进去，结果谁都放不好。**

### 2）不同信息的重要性和时效性不同

例如：

* 用户姓名、偏好：低频变化，适合长期记忆
* 当前任务约束：高优先级，必须本轮常驻
* 旧对话细节：通常不必原文回灌，摘要即可
* 知识库内容：按需检索，不应常驻
* 工具返回：应结构化摘要，而不是原始长文本全塞

### 3）multi-agent 会放大上下文污染

如果每个 agent 都继承完整历史 + 全部 RAG + 全部 memory，会出现：

* token 爆炸
* 子 agent 被不相关信息干扰
* 工具结果重复传递
* 一个 agent 的“草稿思路”污染另一个 agent
* 安全边界模糊

所以多 agent 系统里，**context engineering 比 prompt engineering 重要得多**。LangChain 在多 agent 文章里就明确强调：做多 agent 时，核心不是 agent 数量，而是你是否精确控制每一步的 context 和执行顺序。([LangChain Blog][2])

---

## 三、业内主流思路，可以归纳成 6 条原则

### 原则 1：分层，不要所有信息同权

最常见分层是：

1. **Instruction layer**：系统规则、角色、输出格式
2. **Task layer**：当前用户目标、本轮约束
3. **Working memory**：当前线程的短期状态
4. **External knowledge**：RAG / tools / search
5. **Long-term memory**：用户画像、偏好、长期事实
6. **Execution trace**：计划、已完成步骤、待办项

也就是把“上下文”从一坨文本，变成多层槽位。

---

### 原则 2：能按需取，就不要预加载

Anthropic 在 agent 实践里强调：不要在系统启动时就把所有信息塞进 prompt，而要让 agent 用工具主动拉取相关内容。这个思想特别适合 RAG。([Anthropic][1])

对应到你的系统就是：

* 不要把整个知识库摘要常驻
* 不要把全部长期记忆常驻
* 不要把所有历史对话都带着
* 只在需要时检索、召回、压缩、注入

---

### 原则 3：记忆不是“存更多”，而是“存可重用的结构化事实”

LangGraph 的文档把长期记忆设计成独立于线程状态的持久化 store，并强调长期记忆要以 namespace / key / JSON 文档等形式组织，而不是简单堆聊天记录。([LangChain 文档][3])

这意味着长期记忆更适合存：

* 用户偏好
* 用户背景
* 稳定习惯
* 经常复用的任务约束
* 重要历史事件摘要
* 对系统行为有帮助的明确事实

而不适合存：

* 每轮随口一说的话
* 短时有效的信息
* 冗长原始对话全文

---

### 原则 4：短期记忆要做 trimming + compression

OpenAI 的 context 管理 cookbook 直接把两种常用技术写得很明确：

* **trimming**：裁掉旧消息
* **compression**：把旧消息压成摘要再保留

这也是生产里最稳的基础做法。([OpenAI开发者][4])

---

### 原则 5：RAG 不是只返回 chunk，而要返回“带上下文的 chunk”

Anthropic 在 Contextual Retrieval 中强调，单个 chunk 往往脱离上下文，会丢失它在整篇文档中的位置和语义背景，因此要给 chunk 加“上下文化描述”，同时结合 BM25 / embedding 等多路检索以提升效果。([Anthropic][5])

所以主流 RAG 已经不只是：

* top-k chunk

而是更接近：

* top-k chunk
* 每个 chunk 的来源、标题、章节
* chunk 的简短 contextual summary
* rerank 后结果
* 必要时相邻 chunk 拼接

---

### 原则 6：multi-agent 的关键是“最小必要上下文”

给每个 agent 的不是“大总包”，而是“完成它这一步最少但足够的信息”。

比如：

* Router agent 只看用户请求 + 能力清单 + 少量用户偏好
* Retrieval agent 只看检索 query reformulation 所需信息
* Planner agent 只看任务目标、资源限制、已完成步骤
* Writer agent 只看计划结果 + 已验证资料
* Memory agent 只看是否值得写入长期记忆的候选事实

这比“所有 agent 共享同一大上下文”稳定得多。([LangChain Blog][2])

---

## 四、一个“基础但是功能完备”的 context engineering 方案

下面我给你一个适合 **RAG + 长期记忆 + multi-agent** 的基础版架构。
目标不是最复杂，而是：

* 逻辑清晰
* 方便实现
* 业内套路比较主流
* 后面容易扩展

---

# 方案总览：五层上下文架构

## Layer A：Core Instructions（常驻层）

始终进入上下文，token 要严格控制。

包含：

1. **system prompt**

   * 助手角色
   * 回答风格
   * 安全边界
   * 工具使用原则
   * 引用规则

2. **agent-specific prompt**

   * Router / Planner / Retriever / Writer 各自的职责
   * 输入输出 schema
   * 何时交回控制权

3. **global policies**

   * 不确定时先检索
   * 不得编造引用
   * 优先使用已验证来源
   * 输出前检查是否满足用户要求

这一层要短、稳、少变。

---

## Layer B：Task Context（任务层）

本轮最重要的信息，优先级仅次于 system。

包含：

* 当前用户问题
* 本轮显式约束
* 上一轮未完成任务
* 当前模式（问答 / 写作 / 搜索 / 规划）
* 当前会话目标

建议把它结构化成：

```json
{
  "user_goal": "...",
  "constraints": ["...", "..."],
  "mode": "qa | planning | writing | retrieval",
  "success_criteria": ["..."]
}
```

这样比把多轮用户原话直接丢进去效果更稳。

---

## Layer C：Short-Term Working Memory（线程工作记忆）

这个层对应 LangGraph/agent state 中的 thread-scoped memory。([LangChain 文档][3])

建议包含 4 个槽位：

### C1. recent_messages

只保留最近 N 轮原始对话，比如最近 6~12 条消息。

作用：

* 保持自然多轮连贯
* 保留最近指代关系

### C2. conversation_summary

把更早的对话压成摘要。

建议摘要格式：

```yaml
conversation_summary:
  user_intent_history:
    - ...
  established_constraints:
    - ...
  decisions_made:
    - ...
  unresolved_items:
    - ...
```

### C3. task_state

当前任务执行状态：

```json
{
  "plan": ["步骤1", "步骤2"],
  "completed_steps": ["步骤1"],
  "pending_steps": ["步骤2"],
  "intermediate_results": ["..."]
}
```

### C4. scratchpad_for_system_not_model

注意：这里不是把所有中间推理都暴露给主模型，而是系统内部保留结构化状态。
生产里更推荐存“状态”和“中间结论”，少传“冗长思维链”。

---

## Layer D：Long-Term Memory（持久记忆层）

这个层不要常驻全量注入，而应该 **召回式注入**。

建议拆成两类：

### D1. 用户画像（profile memory）

适合存稳定信息：

* 专业背景
* 语言偏好
* 常见任务类型
* 输出偏好
* 禁忌偏好

例如：

```json
{
  "language": "zh",
  "domain_background": "通信与AI方向研究生",
  "style_preferences": ["准确", "结构清晰", "少空话"],
  "recurring_interests": ["RAG", "Agent", "通信AI"]
}
```

### D2. 历史重要对话（episodic memory）

适合存“以后可能再次用到的关键事件摘要”：

* 某项目的长期设定
* 之前确定的架构方案
* 某次讨论形成的结论
* 用户明确要求“记住”的事项

每条记忆最好包含：

```json
{
  "memory_type": "profile | episodic | preference | project",
  "content": "...",
  "source_session": "...",
  "confidence": 0.92,
  "last_used_at": "...",
  "ttl": "optional",
  "retrieval_tags": ["rag", "multi-agent", "resume"]
}
```

业内主流做法也是把长期记忆当作独立 store，按命名空间/键管理，并按需检索而不是全量灌入。([LangChain 文档][3])

---

## Layer E：External Dynamic Context（动态外部上下文）

这一层包括：

* RAG 检索结果
* Web 搜索结果
* 数据库查询结果
* 工具调用返回
* 文件解析结果

**这一层一定要后处理**，不能把原始返回直接塞给模型。

建议统一标准化成：

```json
[
  {
    "source": "...",
    "type": "rag | web | tool",
    "title": "...",
    "relevance": 0.87,
    "summary": "...",
    "evidence": ["点1", "点2"],
    "raw_excerpt": "必要时才给少量原文"
  }
]
```

Anthropic 关于工具设计也特别强调：工具返回给 agent 的内容应该“有意义且 token-efficient”，而不是原始大块文本。([Anthropic][6])

---

## 五、针对你的系统：推荐的最基础完整流程

下面给你一个非常实用的基础流程。

---

### Step 1：先做请求分类

先由 Router 或前置分类器判断当前请求属于哪类：

* 闲聊 / 轻问答
* 依赖历史上下文
* 依赖用户长期偏好
* 依赖外部知识库
* 需要多步规划
* 需要调用 specialist agent

这一步的目的，是决定后面到底加载哪些 context。

例如：

| 请求类型         | 需要加载的上下文                                                     |
| ------------ | ------------------------------------------------------------ |
| 简单闲聊         | system + recent_messages                                     |
| “继续刚才那个方案”   | system + recent_messages + conversation_summary + task_state |
| “按我一贯风格改简历”  | system + task + profile memory                               |
| “教材里这一章怎么解释” | system + task + RAG                                          |
| “帮我做完整研究方案”  | system + task + summary + profile + RAG + planner state      |

---

### Step 2：先构造最小上下文，不够再补

不要一上来把所有层都加载。

推荐顺序：

1. Core Instructions
2. 当前任务 Task Context
3. recent_messages
4. 如果检测到跨轮依赖，再加 summary / task_state
5. 如果检测到个性化需求，再检索长期记忆
6. 如果检测到知识缺口，再做 RAG / tool retrieval
7. 如果是复杂任务，再交给 planner / specialists

这就是典型的 **progressive context loading**。

---

### Step 3：RAG 和 memory 都用“召回 + 重排 + 压缩”

这是业内最稳的通用模式。

#### 对 RAG：

* query rewrite
* hybrid retrieval（向量 + BM25/关键词）
* rerank
* contextualize chunk
* 最终选 3~6 条高价值证据

Anthropic 的 Contextual Retrieval 就明确强调了上下文化 chunk 和混合检索的价值。([Anthropic][5])

#### 对 memory：

* 当前 query 做 memory retrieval
* 区分 profile / episodic
* 只注入最相关的 1~5 条
* 每条记忆转成简洁事实，而不是原始长对话

---

### Step 4：给不同 agent 发不同上下文包

一个很推荐的做法是做 **Context Builder**，专门给不同 agent 组包。

例如：

#### Router context

```yaml
- system_router_prompt
- current_user_request
- available_agents_and_tools
- minimal_user_profile
```

#### Planner context

```yaml
- system_planner_prompt
- task_context
- conversation_summary
- relevant_constraints
- retrieved_project_memory
```

#### Retriever context

```yaml
- system_retriever_prompt
- focused_query
- user_intent
- query_history_if_needed
```

#### Writer context

```yaml
- system_writer_prompt
- final_task_goal
- approved_plan
- validated_evidence
- style_preferences
```

#### Memory manager context

```yaml
- current_turn_summary
- candidate_facts
- memory_write_policy
```

这样做的好处是：
**agent 之间共享“结果”，而不是共享“全部历史”。**

---

## 六、一个推荐的基础模块设计

你可以把 context engineering 单独做成 4 个组件。

### 1. Context Classifier

负责判断本轮需要哪些上下文源。

输入：

* 用户消息
* 当前线程状态

输出：

* `need_rag`
* `need_profile_memory`
* `need_episodic_memory`
* `need_planner`
* `need_specialist_agent`
* `context_budget`

---

### 2. Memory Manager

负责长期记忆写入与检索。

功能：

* 从对话中抽取值得保存的记忆
* 过滤短时噪声
* 写入 profile / episodic store
* 基于 query 检索相关长期记忆
* 对召回结果去重与压缩

---

### 3. Retrieval Manager

负责外部知识上下文。

功能：

* query rewrite
* 混合检索
* rerank
* chunk contextualization
* citation metadata 附带
* 输出 evidence packet

---

### 4. Context Builder

整个系统最关键的模块。

输入：

* 当前 agent 类型
* task context
* short-term memory
* retrieved long-term memory
* retrieved evidence
* token budget

输出：

* 最终 prompt/context payload

这个模块要做的事情包括：

* 排序
* 去重
* 裁剪
* 压缩
* 模板化结构
* token 预算控制

---

## 七、基础版 token budget 设计

一个功能完备系统，最好从一开始就做 budget，而不是后面补。

可采用这种分配思路：

* 10%：system / policies / agent instructions
* 15%：当前任务与约束
* 20%：recent messages
* 15%：conversation summary / task state
* 15%：长期记忆
* 25%：RAG / tools evidence

不是固定死板，而是优先级驱动：

**当前任务 > 安全规则 > 关键证据 > 最近对话 > 长期偏好 > 旧历史细节**

当超限时，压缩顺序建议是：

1. 先裁掉旧原始对话
2. 保留 summary 替代原始历史
3. 压缩 RAG 证据
4. 只保留最高相关长期记忆
5. 必要时缩短 agent instruction 中的示例

OpenAI 的 session memory 文档中，trimming 和 compression 就是这种思路。([OpenAI开发者][4])

---

## 八、一个比较主流、也最容易落地的基础实现策略

如果你现在真要做一个“基础但完整”的版本，我建议按这个最小闭环来：

### 第一阶段：单 agent + context builder

先不要急着上很多 agent。

先做：

* recent history
* conversation summary
* profile memory
* episodic memory
* RAG retrieval
* unified context builder

先把 **“一个 agent 的上下文编排”** 做稳。

---

### 第二阶段：加入 router + specialist

再加两个最常见角色：

* Router
* Specialist（如 writer / analyst / tutor）

并且强制规则：

* specialist 不直接继承完整会话
* specialist 只接收 context builder 组装后的最小包

---

### 第三阶段：加入 memory write-back

每轮结束后做：

1. 生成 turn summary
2. 提取可持久化事实
3. 判断是否写入长期记忆
4. 更新 conversation summary
5. 更新 task state

这是很多系统从“能回答”到“会持续变聪明”的关键分水岭。LangGraph/LangMem 一类方案也都强调长期记忆应作为一个独立写回流程来管理。([LangChain 文档][3])

---

## 九、给你一个实用的上下文模板

下面这个模板很适合作为基础版：

```yaml
<System>
你是...
全局规则：
- ...
- ...
- ...

<AgentRole>
当前角色：Writer
职责：
- 根据已验证资料输出最终答案
- 不自行编造来源
- 若证据不足，明确说明

<TaskContext>
user_goal: ...
constraints:
  - ...
success_criteria:
  - ...

<ShortTermMemory>
recent_messages:
  - ...
conversation_summary:
  - 已讨论...
  - 已确认...
task_state:
  completed:
    - ...
  pending:
    - ...

<LongTermMemory>
user_profile:
  - 用户偏好中文回答
  - 用户偏好结构清晰、技术细致
episodic_memory:
  - 上次已确定该项目采用 RAG + memory + router 结构

<ExternalEvidence>
- source: doc_1
  relevance: 0.92
  summary: ...
  evidence:
    - ...
    - ...
- source: web_2
  relevance: 0.88
  summary: ...

<OutputInstruction>
输出要求：
- 先给结论，再展开
- 对引用内容标注来源
- 不确定时明确说明
```

这个模板的核心价值不是“格式漂亮”，而是让模型知道：
**什么是规则，什么是当前任务，什么是历史状态，什么是外部证据。**

---

## 十、几个很常见的错误设计

### 错误 1：把长期记忆当聊天记录仓库

后果：

* 召回噪声大
* 命中率低
* 上下文污染严重

正确做法：

* 长期记忆存“抽取后的事实”和“事件摘要”

---

### 错误 2：RAG 直接塞 top-k 原文

后果：

* token 浪费
* 证据重复
* chunk 脱离上下文

正确做法：

* rerank + contextual summary + 少量关键原文

---

### 错误 3：每个 agent 继承完整上下文

后果：

* 成本高
* 性能不稳
* 多 agent 反而更差

正确做法：

* 给每个 agent 单独的最小必要上下文包

---

### 错误 4：只做聊天历史截断，不做摘要

后果：

* 系统很快“失忆”
* 老约束丢失

正确做法：

* 原始历史 + 摘要并存
* 老消息压成状态摘要

---

### 错误 5：把 context engineering 只当 prompt 优化

后果：

* 一直在改 wording，但系统稳定性上不去

正确做法：

* 把它当作 **状态管理 + 检索编排 + token 预算 + 信息分层** 的工程问题

---

## 十一、如果参考业内主流，我会推荐你优先借鉴这几类思想

### 1. Anthropic 风格

核心思想：

* agent 启动时不要预塞过多信息
* 按需检索
* 强调 tool 返回内容的质量和简洁性
* context engineering 比单纯 prompt 更关键
* RAG 要做 contextual retrieval

适合你系统里的：

* RAG
* tool 结果压缩
* 动态上下文加载

([Anthropic][1])

### 2. LangGraph / LangChain 风格

核心思想：

* 区分短期记忆和长期记忆
* 长期记忆独立 store 化
* 线程状态单独维护
* 多 agent / graph 节点每步显式控制上下文

适合你系统里的：

* thread state
* long-term memory store
* agent state machine

([LangChain 文档][3])

### 3. OpenAI agent/context 管理风格

核心思想：

* trimming + compression
* session-based context management
* agents/tool orchestration
* 生产里注重上下文成本与可靠性平衡

适合你系统里的：

* 长对话管理
* 历史压缩
* agent SDK 式上下文注入

([OpenAI开发者][4])

---

## 十二、最后给一个最实用的结论

对于一个包括 **RAG + 长期记忆 + multi-agent** 的系统，最基础但功能完备的 context engineering，不是“写一个很长的系统提示词”，而是至少具备这 7 个能力：

1. **上下文分层**：system / task / short-term / long-term / evidence 分开
2. **按需加载**：不是所有内容都常驻
3. **历史压缩**：recent + summary 双轨制
4. **长期记忆结构化**：profile 和 episodic 分开
5. **RAG 后处理**：检索、重排、上下文化、压缩
6. **按 agent 组包**：最小必要上下文，而非全量共享
7. **token budget 控制**：超限时按优先级裁剪

你可以把它记成一句话：

> **Context engineering = 给对的 agent，在对的时机，提供对的那一小撮信息。**

这比“尽量把信息都给模型”更接近业内主流，也更适合真正可扩展的 agent 系统。([Anthropic][1])

如果你愿意，我下一步可以直接给你继续细化成一份 **可落地的软件架构设计**，例如：
**模块图 + memory schema + context builder 伪代码 + multi-agent 的上下文流转图**。

[1]: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents?utm_source=chatgpt.com "Effective context engineering for AI agents"
[2]: https://blog.langchain.com/how-and-when-to-build-multi-agent-systems/?utm_source=chatgpt.com "How and when to build multi-agent systems"
[3]: https://docs.langchain.com/oss/python/langgraph/memory?utm_source=chatgpt.com "Memory overview"
[4]: https://developers.openai.com/cookbook/examples/agents_sdk/session_memory/?utm_source=chatgpt.com "Context Engineering - Short-Term Memory Management ..."
[5]: https://www.anthropic.com/news/contextual-retrieval?utm_source=chatgpt.com "Contextual Retrieval in AI Systems"
[6]: https://www.anthropic.com/engineering/writing-tools-for-agents?utm_source=chatgpt.com "Writing effective tools for AI agents—using ..."
