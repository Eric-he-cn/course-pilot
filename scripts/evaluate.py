#!/usr/bin/env python3
"""离线评测入口：从 trace 抽样，用 judge 模型打分（架构 §16.2 的 online sample 层）。

judge 与用户会话完全隔离：独立提示词、独立调用、只读 trace，不写任何业务数据。
判分维度对应策划书 §6：讲解是否忠于教材、概念归因是否正确。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from adapters.llm.deepseek import DeepSeekAgentChat  # noqa: E402
from contracts.llm import ChatDelta, ChatMessage  # noqa: E402
from core.settings import Settings  # noqa: E402

_JUDGE_PROMPT = """你是学习助手的离线评审。只依据给出的记录打分，不做补充解释。

按三个维度各给 1-5 分（5 最好），并给一句理由：
- faithfulness：回答里标注了教材引用编号的结论，是否确实能由检索到的证据支持；
  没有引用任何证据的通用知识回答按"不适用"给 3 分。
- attribution：本轮写入的概念归因是否与题目/讨论的考点一致；没有归因时给 3 分。
- usefulness：对学生是否真的有用（针对性、可操作、不含无意义的过程叙述）。

只输出 JSON：{"faithfulness": n, "attribution": n, "usefulness": n, "reason": "一句话"}"""


def load_records(traces_dir: Path, *, limit: int, seed: int) -> list[dict]:
    records: list[dict] = []
    for path in sorted(traces_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    usable = [record for record in records if record.get("status") == "completed" and record.get("answer_chars")]
    random.Random(seed).shuffle(usable)
    return usable[:limit]


def render_case(record: dict, store_path: Path) -> str:
    """把 trace 记录还原成 judge 可读的一段材料。"""
    import sqlite3

    connection = sqlite3.connect(store_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT role, content, citations_json FROM messages WHERE turn_id = ? ORDER BY created_at",
        (record.get("turn_id"),),
    ).fetchall()
    question = next((row["content"] for row in rows if row["role"] == "user"), "（未找到提问）")
    answer_row = next((row for row in rows if row["role"] == "assistant"), None)
    answer = answer_row["content"] if answer_row else "（未找到回答）"
    citations = json.loads(answer_row["citations_json"]) if answer_row else []
    evidence = "\n".join(f"[{item.get('number')}] {item.get('document')} p{item.get('page')}：{item.get('snippet', '')[:200]}" for item in citations) or "（本轮没有引用教材证据）"
    concepts = connection.execute(
        "SELECT kind, COALESCE(c.name, e.topic_hint) AS target FROM evidence_events e LEFT JOIN concepts c ON c.id = e.concept_id"
        " WHERE e.course_id = ? AND e.created_at >= ? ORDER BY e.created_at",
        (record.get("resolution", {}).get("course_id"), record.get("started_at", "")),
    ).fetchall()
    attribution = "\n".join(f"- {row['kind']} → {row['target']}" for row in concepts) or "（本轮没有写入概念归因）"
    connection.close()
    return (
        f"学生提问：\n{question[:1200]}\n\n"
        f"检索到并被引用的教材证据：\n{evidence}\n\n"
        f"助手回答：\n{answer[:2500]}\n\n"
        f"本轮写入的概念归因：\n{attribution}"
    )


def judge(chat: DeepSeekAgentChat, case: str) -> dict:
    messages = [ChatMessage(role="system", content=_JUDGE_PROMPT), ChatMessage(role="user", content=case)]
    parts: list[str] = []
    for item in chat.chat(messages=messages):
        if isinstance(item, ChatDelta):
            parts.append(item.text)
    raw = "".join(parts).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        return {"error": "judge 未返回 JSON", "raw": raw[:200]}
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {"error": "judge JSON 解析失败", "raw": raw[:200]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(ROOT / "data"), help="被评测实例的数据目录")
    parser.add_argument("--limit", type=int, default=5, help="抽样条数")
    parser.add_argument("--seed", type=int, default=0, help="抽样随机种子，便于复现")
    parser.add_argument("--out", default="", help="可选，把逐条结果写成 JSONL")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    settings = Settings.from_environment()
    if not settings.remote_llm_configured:
        raise SystemExit("judge 需要配置 TEXT_API_KEY / TEXT_BASE_URL / TEXT_MODEL")

    records = load_records(data_dir / "traces", limit=args.limit, seed=args.seed)
    if not records:
        raise SystemExit(f"{data_dir / 'traces'} 里没有可评测的完成轮次")

    chat = DeepSeekAgentChat(
        api_key=settings.text_api_key, base_url=settings.text_base_url, model=settings.text_model,
        total_timeout_seconds=settings.llm_total_timeout_seconds,
    )
    results, scored = [], {"faithfulness": [], "attribution": [], "usefulness": []}
    try:
        for record in records:
            verdict = judge(chat, render_case(record, data_dir / "coursepilot.db"))
            entry = {
                "turn_id": record.get("turn_id"), "prompt_version": record.get("prompt_version"),
                "skill": (record.get("skill") or {}).get("name"), "model": (record.get("responder") or {}).get("model"),
                **verdict,
            }
            results.append(entry)
            for key in scored:
                if isinstance(verdict.get(key), (int, float)):
                    scored[key].append(float(verdict[key]))
            print(f"{entry['turn_id']} | prompt={entry['prompt_version']} skill={entry['skill']} → "
                  + ", ".join(f"{key}={verdict.get(key)}" for key in scored) + f" | {verdict.get('reason', verdict.get('error', ''))}")
    finally:
        chat.close()

    print("\n按维度平均分（同一 prompt_version 内才可比）：")
    for key, values in scored.items():
        print(f"  {key:14} {sum(values) / len(values):.2f}（{len(values)} 条）" if values else f"  {key:14} 无有效评分")
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in results) + "\n", encoding="utf-8")
        print(f"逐条结果已写入 {args.out}")


if __name__ == "__main__":
    main()
