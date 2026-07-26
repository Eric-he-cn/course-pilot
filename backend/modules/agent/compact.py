"""对话压缩：把较早的消息换成一份结构化摘要，让长会话不再从最旧的开始硬丢。

提示词结构照搬 Claude Code 的 compact prompt（两阶段 analysis/summary、九段结构、
NO_TOOLS 前后夹），只把明显属于 coding 域的名词换成教材与错题。
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from contracts.llm import AgentChatPort, ChatFinal, ChatMessage, LLMProviderError

# 摘要提示词版本：改动就要 +1，trace 里据此对比摘要质量。
COMPACT_PROMPT_VERSION = "compact_v1"
# 摘要自身也占每一轮的上下文，必须有上界，否则二次压缩会让它单调变长。
SUMMARY_MAX_CHARS = 12_000

_NO_TOOLS_PREAMBLE = """关键：只用纯文本回复。不要调用任何工具。

- 不要使用检索、资料清单、概念目录、证据记录或任何其他工具。
- 你在上面的对话里已经拥有所需的全部上下文。
- 工具调用会被拒绝，并浪费你唯一的一轮机会——你会任务失败。
- 你的整个回复必须是纯文本：一个 <analysis> 块，后跟一个 <summary> 块。
"""

_ANALYSIS_INSTRUCTION = """在给出最终摘要之前，先把你的分析包在 <analysis> 标签里，用来梳理思路、确保覆盖所有必要的点。分析过程中：

1. 按时间顺序逐条分析对话中的每条消息和每个环节。对每个环节都要充分识别：
   - 用户明确提出的请求和意图
   - 你处理用户请求的思路
   - 关键决策、学科概念和讲解方式
   - 具体细节，例如：
     - 教材文件名与页码
     - 完整的教材原文片段
     - 定义与公式
     - 引用编号
   - 用户答错的题目，以及你是怎么纠正的
   - 特别留意你收到的具体用户反馈，尤其是用户要求你换种做法的地方。
2. 复核事实上的准确性和完整性，把每个必需要素都充分覆盖。
"""

_BASE_PROMPT = """你的任务是给到目前为止的辅导对话写一份详细摘要，密切关注用户明确提出的请求和你之前采取的行动。
这份摘要要充分捕捉学科细节、教材引用和讲解脉络——这些是在不丢失上下文的前提下继续辅导所必需的。

{analysis_instruction}
你的摘要应包含以下各段：

1. 主要请求与意图：详细捕捉用户明确提出的全部请求和意图。
2. 关键概念与结论：列出讨论过的所有重要学科概念、定义和公式。
3. 教材与引用片段：列举查阅、引用过的具体教材文件与页码。特别关注最近的消息，适当处附上完整的教材原文片段，并说明这段引用为什么重要。
4. 错题与纠正：列出用户答错或卡住的题目，以及正确结论是什么。特别留意你收到的具体用户反馈，尤其是用户要求你换种做法的地方。
5. 问题求解：记录已经讲明白的困惑，和仍在卡着的地方。
6. 全部用户消息：列出所有非工具结果的用户消息。这些对理解用户反馈和意图变化至关重要。
7. 待办任务：概述你被明确要求去做的、尚未完成的任务。
8. 当前工作：详细描述在本次摘要请求之前正在做什么，特别关注用户和助手双方最近的消息。适当处附上教材文件名与原文片段。
9. 可选的下一步：列出你接下来要做的、与最近工作相关的一步。重要：确保这一步与用户最近一次明确请求、以及本次摘要请求前正在做的任务直接一致。如果上一个任务已经收尾，那么只有当下一步明确符合用户请求时才列出。不要在未与用户确认的情况下，擅自去做无关请求，或去做那些早已完成的旧请求。
   如果确有下一步，附上最近对话的原话直接引用，准确显示当时在做什么任务、停在哪里。这必须逐字照抄，以确保任务理解不发生漂移。

以下是你输出应有的结构示例：

<example>
<analysis>
[你的思考过程，确保所有要点都被充分、准确地覆盖]
</analysis>

<summary>
1. 主要请求与意图：
   [详细描述]

2. 关键概念与结论：
   - [概念 1]
   - [概念 2]
   - [...]

3. 教材与引用片段：
   - [教材文件名，第 N 页]
      - [这段引用为何重要]
      - [教材原文片段]
   - [...]

4. 错题与纠正：
    - [题目与用户的错答]：
      - [正确结论]
      - [用户对此的反馈（若有）]
    - [...]

5. 问题求解：
   [已讲明白的困惑与仍然卡着的地方]

6. 全部用户消息：
    - [详细的、非工具调用的用户消息]
    - [...]

7. 待办任务：
   - [任务 1]
   - [...]

8. 当前工作：
   [当前工作的精确描述]

9. 可选的下一步：
   [要采取的可选下一步]

</summary>
</example>

请基于到目前为止的对话，按此结构给出摘要，确保回复的精确与充分。

## 摘要指令

掌握度数值、证据事件、复习排期、学习计划与长期记忆都由服务端单独持久化，不依赖这份摘要保真——
不要复述掌握度百分比或复习日期。摘要要留住的是叙述性上下文：用户想学什么、讲到哪、
哪里没懂、引用过哪些教材位置。

提醒：不要调用任何工具。只用纯文本回复——一个 <analysis> 块，后跟一个 <summary> 块。工具调用会被拒绝，你会任务失败。
"""

_SUMMARY_BLOCK = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)


def build_prompt() -> str:
    return _NO_TOOLS_PREAMBLE + "\n" + _BASE_PROMPT.format(analysis_instruction=_ANALYSIS_INSTRUCTION)


def extract_summary(text: str) -> str | None:
    """只认 <summary> 块。解析不出就返回 None——宁可这轮不压缩，也不能把
    未经校验的整段回复（可能含 analysis 或拒答）写成摘要：水位一旦生效，
    那批原文就再也不进上下文了。"""
    match = _SUMMARY_BLOCK.search(text)
    body = (match.group(1) if match else "").strip()
    if not body:
        return None
    return body[:SUMMARY_MAX_CHARS]


@dataclass(frozen=True)
class CompactionInput:
    """要压缩的消息，以及此前那份摘要（二次压缩时把它一起喂进去）。"""
    transcript: Sequence[tuple[str, str]]
    previous_summary: str = ""


# 保留原文的比例：这部分最近的消息不进摘要，仍以原文进上下文。
KEEP_RATIO = 0.3


def summarize(*, responder: AgentChatPort, payload: CompactionInput) -> tuple[str | None, str]:
    """返回（摘要, 失败原因）。失败原因非空时调用方保持原样、不落库。

    增量绝不能外泄给用户——两阶段提示词的 <analysis> 就在里面。
    """
    blocks = [f"{'用户' if role == 'user' else '助教'}：{content}" for role, content in payload.transcript]
    if not blocks:
        return None, "empty_transcript"
    history = "\n\n".join(blocks)
    if payload.previous_summary:
        history = f"[此前对话的摘要]\n{payload.previous_summary}\n\n[之后的对话]\n{history}"
    messages = [
        ChatMessage(role="system", content=build_prompt()),
        ChatMessage(role="user", content=f"以下是需要压缩的辅导对话：\n\n{history}"),
    ]
    parts: list[str] = []
    final: ChatFinal | None = None
    try:
        for item in responder.chat(messages=messages, tools=()):
            if isinstance(item, ChatFinal):
                final = item
                break
            text = getattr(item, "text", None)
            if text is None:
                # 无工具调用时不该出现 tool_calls 终态；出现就当失败，不猜它想干什么。
                return None, "unexpected_tool_calls"
            parts.append(text)
    except LLMProviderError as error:
        # 中途中断时手上只有一段被截断的摘要，不能用。
        return None, f"provider_error:{error.code}"
    if final is None:
        return None, "no_final_response"
    summary = extract_summary("".join(parts) or final.text)
    if summary is None:
        return None, "summary_not_parsed"
    return summary, ""
