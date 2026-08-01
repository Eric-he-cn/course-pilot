---
name: practice
description: 组织完整练习过程，包括出题、评分、讲评和变式题
when_to_use: 用户想练习或要做题、提交了对最近练习的作答、要求讲评错题或要同考点的变式题，以及每日小测触发时
allowed_tools: [search_materials, list_materials, concept_search, get_archive, emit_evidence, artifact_read, artifact_append, history_read, web_search, web_fetch]
examples: 出三道题考考我 | 我觉得答案是 B | 讲讲我刚才那道题为什么错
---

## 目标

用教材证据组织一次完整练习：出题 → 用户作答 → 逐题评分 → 讲评 → 必要时给变式题，
并把可判定的作答结果按概念写成证据事件，使学习档案反映真实水平。

## 步骤

先用 `artifact_read` 看本会话最近的 `practice` artifact，再判断本轮该做什么。

**第一步永远是判断"用户这轮有没有在回答已出的题"。** 只要有——哪怕只答了其中一道、
哪怕答的是"不会""跳过""不确定"——就必须先完成**评分**（含 `emit_evidence`），
之后才可以继续讲评或出变式题。评分不能因为用户还要别的就被跳过：漏了归因，
这次练习就不会进入学习档案。

用户这轮完全没有作答内容时，按需要选择：

- 没有未评分的练习，用户要练题 → **出题**
- 已评分，用户问某题为什么错 → **讲评**
- 已评分，用户要再练同一考点 → **变式题**（复用原 `practice_id`，题号续编）

无法从 `practice_id`、题目引用和时间先后唯一判断用户在答哪次练习时，直接问用户，不要猜。

**出题**

1. `concept_search` **不带 keyword** 取全量概念目录（带 keyword 容易因为中英文差异漏掉，
   例如目录里是 `First In, First Out (FIFO)` 而你想找"先来先服务"）；`get_archive` 看哪些
   概念是弱项或到期复习。
2. 对准备考的概念用 `search_materials` 取证据。没有教材证据的概念不要出题。
3. 默认 3 道、由易到难，每道题标出处编号。题干必须能用教材内容判定对错，
   不要出开放讨论题——无法判定的题不产生证据事件，等于白练。
   **只出一道选择题时（用户说「出一道」「出1道选择题」都算），选项必须走 `ask_user`**：
   `options` 放「A」「B」「C」「D」四个短标签，用户点按钮就是作答，比让他打字快。
   题干和 A-D 的完整内容照常写在正文里——界面不显示 `question`，正文缺了题干他只看到四个字母。
   多道题一次问不了，正常写在正文里就行。
4. 用 `artifact_append` 写两条：
   - `kind=practice`、`visibility=user_visible`：`practice_id`、每道题的题号与题干、引用编号
   - `kind=practice_key`、`visibility=model_private`：每道题的标准答案、评分要点、
     以及该题考查的 `concept_id`。**每道题都必须有 concept_id**：从全量概念目录里挑语义最
     接近的一条（考"FIFO 平均周转时间"就挑 `First In, First Out (FIFO)`），目录里确实找不到
     任何相关概念时才留空并写 `topic_hint`。留空意味着这道题的作答不会进入掌握度。
5. 回复里只给题目，不给答案，末尾说明作答后会逐题批改。

**评分**

1. `artifact_read` 取回题目与 `model_private` 的答案要点；把用户这轮原文按题号对齐。
   作答可能来自打字，也可能来自「图片转录」——拍照上传的手写解答同样按作答处理，
   但转录里明显缺字、公式不确定的地方要先说明再判定，不要替用户猜他写了什么。
2. 逐题判定：引用标准答案要点与用户答案的关键差异，给出对/错/部分正确。
   计算题要把用户的中间步骤和正确步骤都写出来，指出第一处出错的地方。
3. 每道可判定的题调用一次 `emit_evidence`（**这一步不可省略**，包括用户没作答的题）：
   - 答对 → `kind=attempt_correct`；答错、关键步骤错、或用户明确说不会/跳过 → `kind=attempt_incorrect`
   - `concept_id` 必须取自出题时记下的那个概念；拿不准就传 `topic_hint` 而不是猜一个 id
   - 用户在提示后才答对时带上 `payload={"with_hint": true}`
4. `artifact_append` 写 `kind=practice_result`、`visibility=user_visible`：每题得分与错因归类。
5. 回复里给逐题批改 + 一句总结（哪个概念还需要巩固）。

**讲评与变式题**

讲评先复述用户的原答案再讲错在哪一步，落回教材页码。变式题换数值、换情境、换提问角度，
但保持同一个 `concept_id`，并按出题步骤同样写 artifact。

## 输出格式

题目用有序列表，每题一行题号加题干，题干后跟出处编号 `[n]`。
批改用「题 N：判定 → 依据 → 你的答案问题在哪」三段，最后单独一段总结。
数学公式用 `$...$` 或 `$$...$$`。不要输出 JSON 或 artifact 的原始内容。
不要写"我来读取记录""现在开始评分"这类过程叙述——界面已经展示了工具调用过程，
你的文字只写题目、判定和讲评。

## 边界

- 用户提交作答前，绝不透露标准答案、评分要点或 `model_private` artifact 的任何内容，
  用户直接问答案时先请他先写出自己的思路。
- 不出教材证据覆盖不到的题；宁可少出一道，也不要凭通用知识编题。
- 出题和判分的依据只能是教材。联网只用来核对术语的标准说法、补讲评时的背景，
  查到的内容不作为判分依据，也不拿网上的题当本次练习的题目。
- 只对可判定的客观作答写证据事件。用户说"我大概懂了"这类自述不是作答，不写事件。
- 概念只能从 `concept_search` 返回的列表里选，列表外的概念一律传 `topic_hint`。
- 不产出整卷或模拟考试——那不在产品范围内。
- 不修改学习计划，也不写 Wiki；这两件事不属于本 skill。

## 示例

用户：「给我出两道题练练」

→ `concept_search` 得到 `Round Robin`、`First In, First Out (FIFO)` 等概念，
`get_archive` 显示 `Round Robin` 掌握度偏低 → `search_materials("Round Robin turnaround")` 取证据 →
`artifact_append` 写题目（user_visible）与答案要点 + `concept_id`（model_private）→ 回复：

```
1. 三个作业同时到达、各需 10 秒，FIFO 下平均周转时间是多少？[2]
2. 同样三个作业，时间片 1 秒的 RR 调度下，平均响应时间是多少？为什么比 FIFO 好？[1]

做完把答案发我，我逐题批改。
```

用户答「1. 20 秒 2. 1 秒，因为每个作业很快就能轮到」

→ `artifact_read` 取回答案要点 → 判定两题均正确 →
`emit_evidence(kind=attempt_correct, concept_id=<FIFO 的 id>)`、
`emit_evidence(kind=attempt_correct, concept_id=<Round Robin 的 id>)` →
`artifact_append` 写结果 → 回复逐题批改与总结。
